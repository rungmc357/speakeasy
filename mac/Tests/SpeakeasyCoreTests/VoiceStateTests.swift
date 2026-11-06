import Foundation
import XCTest
@testable import SpeakeasyCore

final class VoiceStateTests: XCTestCase {
    let t0 = Date(timeIntervalSince1970: 1_800_000_000)

    func run(_ events: [VoiceEvent], from state: VoiceState = VoiceState(), start: Date? = nil, step: TimeInterval = 0.1) -> VoiceState {
        var s = state
        var now = start ?? t0
        for event in events { s = reduce(s, event, now: now); now += step }
        return s
    }

    func live() -> VoiceState {
        var s = VoiceState()
        s.idleTimeout = 5 * 60   // these tests use a 5-minute auto-pause (the default is 15)
        return run([.startRequested, .sessionAdmitted(interactionID: "vi_1"), .sessionStarted], from: s)
    }

    func work(_ status: String = "working", short: String? = "Checking UA 123 status", at: Date? = nil,
              stale: Bool = false, run: String = "run_1", result: WorkResult? = nil) -> WorkInfo {
        let stamp = at ?? t0
        return WorkInfo(runID: run, status: status, stale: stale, updated: stamp, shortStatus: short,
                        detail: "Looking up today's departure", updatedAt: stamp, statusSource: "authored",
                        events: [WorkEventItem(kind: "request", text: "Is my flight on time?", at: t0)],
                        result: result)
    }

    func testIdleFiveMinutesExpiresOnlyWhenQuiet() {
        var s = live()   // sessionStarted at ~t0
        XCTAssertFalse(s.idleExpired(at: t0 + 299))
        XCTAssertTrue(s.idleExpired(at: t0 + 301))
        // Speech from either side resets the clock.
        s = reduce(s, .inputDelta("hey there"), now: t0 + 200)
        XCTAssertFalse(s.idleExpired(at: t0 + 301))
        s = reduce(s, .outputDelta("Sure."), now: t0 + 250)
        s = reduce(s, .tick, now: t0 + 260)   // speaking clears after the quiet gap
        XCTAssertFalse(s.idleExpired(at: t0 + 540))
        XCTAssertTrue(s.idleExpired(at: t0 + 551))
        // A cough is not speech.
        var c = live()
        c = reduce(c, .outputDelta("Done."), now: t0 + 10)
        c = reduce(c, .tick, now: t0 + 20)
        c = reduce(c, .inputDelta("[cough]"), now: t0 + 200)
        XCTAssertTrue(c.idleExpired(at: t0 + 311))
    }

    func testIdleNeverFiresWhileSpeakingAwaitingApprovalOrNotLive() {
        var s = live()
        s = reduce(s, .outputDelta("Long answer"), now: t0 + 1)
        XCTAssertFalse(s.idleExpired(at: t0 + 302))   // still speaking (no tick yet)
        var a = live()
        a = reduce(a, .approval(ApprovalInfo(runID: "r", requestID: "q", description: "OK?")), now: t0 + 1)
        XCTAssertFalse(a.idleExpired(at: t0 + 1000))
        XCTAssertFalse(VoiceState().idleExpired(at: t0 + 1000))
        var p = live()
        p = reduce(p, .pauseRequested, now: t0 + 1)
        XCTAssertFalse(p.idleExpired(at: t0 + 1000))
    }

    func testConnectingToLive() {
        var s = reduce(VoiceState(), .startRequested, now: t0)
        XCTAssertEqual(s.connection, .connecting)
        s = reduce(s, .sessionStarted, now: t0)
        XCTAssertEqual(s.connection, .live)
        XCTAssertEqual(present(s).primary, "Listening")
    }

    func testWorkEventWhileSpeakingKeepsSpeaking() {
        var s = run([.outputDelta("Let me check that.")], from: live())
        guard case .speaking = s.speech else { return XCTFail("expected speaking") }
        s = reduce(s, .work(work()), now: t0 + 1)
        s = reduce(s, .interaction(InteractionSnapshot(status: "working", runID: "run_1")), now: t0 + 1.5)
        s = reduce(s, .approval(nil), now: t0 + 2)
        guard case .speaking = s.speech else { return XCTFail("work events must not change speech") }
        XCTAssertEqual(present(s).primary, "Hermes")
        XCTAssertEqual(present(s).mark, .speaking)
        guard case .active(let short, _, _, _) = s.work else { return XCTFail("expected active work") }
        XCTAssertEqual(short, "Checking UA 123 status")
    }

    func testSpeechClearsAfterSixSecondQuietGap() {
        var s = reduce(live(), .outputDelta("Hello"), now: t0)
        s = reduce(s, .tick, now: t0 + 5.9)
        guard case .speaking = s.speech else { return XCTFail("still speaking before 6 s") }
        s = reduce(s, .outputDelta(" there"), now: t0 + 5.9)
        s = reduce(s, .tick, now: t0 + 11)
        guard case .speaking = s.speech else { return XCTFail("a new delta restarts the gap") }
        s = reduce(s, .tick, now: t0 + 12)
        XCTAssertEqual(s.speech, .idle)
        XCTAssertEqual(present(s).primary, "Listening")
    }

    func testMuteToggling() {
        var s = reduce(live(), .toggleMic, now: t0)
        XCTAssertEqual(s.mic, .muted)
        XCTAssertFalse(s.localAudioEnabled)
        XCTAssertEqual(s.connection, .live, "mute keeps the (billed) call open")
        let p = present(s)
        XCTAssertEqual(p.primary, "Muted"); XCTAssertEqual(p.micLabel, "Muted"); XCTAssertTrue(p.micMuted)
        XCTAssertEqual(p.secondary, "Mic off · call still open")
        s = reduce(s, .toggleMic, now: t0 + 1)
        XCTAssertEqual(s.mic, .live)
        XCTAssertEqual(present(s).micLabel, "Mic on")
        // Mute is ignored before a call exists.
        XCTAssertEqual(reduce(VoiceState(), .toggleMic, now: t0).mic, .live)
    }

    func testMuteDoesNotHideSpeaking() {
        var s = reduce(live(), .toggleMic, now: t0)
        s = reduce(s, .outputDelta("Still talking"), now: t0 + 0.5)
        XCTAssertEqual(present(s).primary, "Hermes")
        XCTAssertEqual(present(s).micLabel, "Muted")
    }

    func testQuietReplyDisablesRemoteAudioUntilUserSpeaks() {
        var s = reduce(live(), .outputDelta("A long answer"), now: t0)
        s = reduce(s, .quietAssistant, now: t0 + 1)
        XCTAssertEqual(s.speech, .quieted)
        XCTAssertFalse(s.remoteAudioEnabled)
        s = reduce(s, .outputDelta(" continues"), now: t0 + 2)
        XCTAssertEqual(s.speech, .quieted, "further output stays quiet")
        s = reduce(s, .inputDelta("Next"), now: t0 + 3)
        XCTAssertEqual(s.speech, .idle)
        XCTAssertTrue(s.remoteAudioEnabled)
    }

    func testTranscriptResetsOnFirstUserDeltaAfterReply() {
        var s = run([.inputDelta("What did we "), .inputDelta("decide?"), .outputDelta("We kept "), .outputDelta("one popup.")], from: live())
        XCTAssertEqual(s.exchange.you, "What did we decide?")
        XCTAssertEqual(s.exchange.assistant, "We kept one popup.")
        s = reduce(s, .inputDelta("And next?"), now: t0 + 10)
        XCTAssertEqual(s.exchange.you, "And next?")
        XCTAssertEqual(s.exchange.assistant, "")
        s = reduce(s, .inputDelta(" Please"), now: t0 + 10.5)
        XCTAssertEqual(s.exchange.you, "And next? Please", "no reset before a reply starts")
    }

    func testLateTailOfMySentenceIsNotErasedWhenAssistantStartsTalking() {
        var s = reduce(live(), .inputDelta("Can you check the weather "), now: t0)
        s = reduce(s, .outputDelta("Sure, "), now: t0 + 0.8)
        s = reduce(s, .inputDelta("for this weekend?"), now: t0 + 1.2)
        XCTAssertEqual(s.exchange.you, "Can you check the weather for this weekend?")
        XCTAssertEqual(s.exchange.assistant, "Sure, ", "the reply is not wiped by my trailing words")
        XCTAssertEqual(s.transcript.map(\.text), ["Can you check the weather for this weekend?", "Sure, "])
    }

    func testTranscriptKeepsEveryTurnAcrossExchanges() {
        var s = run([.inputDelta("What did we decide?"), .outputDelta("We kept one popup.")], from: live())
        s = reduce(s, .inputDelta("And next?"), now: t0 + 10)
        s = reduce(s, .outputDelta("Ship it."), now: t0 + 11)
        XCTAssertEqual(s.transcript.map(\.speaker), [.you, .assistant, .you, .assistant])
        XCTAssertEqual(s.transcript.map(\.text), ["What did we decide?", "We kept one popup.", "And next?", "Ship it."])
        XCTAssertEqual(s.exchange.you, "And next?", "the live caption still shows only the latest exchange")
    }

    func testCoughAfterReplyAddsNothingToTranscript() {
        var s = run([.inputDelta("Hi"), .outputDelta("Hey.")], from: live())
        s = reduce(s, .inputDelta("[cough]"), now: t0 + 10)
        XCTAssertEqual(s.transcript.count, 2)
    }

    func testDelegationShowsHonestWaiting() {
        var s = reduce(live(), .delegationCreated, now: t0)
        guard case .waiting = s.work else { return XCTFail("expected waiting") }
        s = reduce(s, .tick, now: t0 + 32)
        XCTAssertEqual(present(s).secondary, "Waiting for Hermes · 32s")
        XCTAssertEqual(present(s).tone, .plain)
    }

    /// Live report: "Waiting for Claire" sat there for minutes because the task never started.
    func testAHandoffThatNeverBecomesATaskSaysSoInsteadOfWaitingForever() {
        var s = reduce(live(), .delegationCreated, now: t0)
        s = reduce(s, .tick, now: t0 + VoiceState.notReceivedAfter + 1)
        guard case .notReceived = s.work else { return XCTFail("expected notReceived, got \(s.work)") }
        XCTAssertEqual(present(s).secondary, "Hermes didn't get that")
        XCTAssertEqual(present(s).tone, .warning)
        // a task that shows up late still takes over
        s = reduce(s, .work(work(at: t0 + 50)), now: t0 + 50)
        guard case .active = s.work else { return XCTFail("expected active once the task appears") }
    }

    func testARunningTaskWaitingOnApprovalNeverTimesOut() {
        var s = reduce(live(), .delegationCreated, now: t0)
        s = reduce(s, .work(work("waiting_for_approval", at: t0 + 2)), now: t0 + 2)
        s = reduce(s, .tick, now: t0 + 600)
        if case .notReceived = s.work { XCTFail("a real task must never read as not received") }
    }

    func testFreshActiveWorkGlimmersThenGoesPlainThenStale() {
        var s = reduce(live(), .work(work(at: t0)), now: t0 + 1)
        XCTAssertEqual(present(s).tone, .glimmer)
        s = reduce(s, .tick, now: t0 + 60)
        XCTAssertEqual(present(s).tone, .plain, "aging status stops glimmering")
        XCTAssertEqual(present(s).secondary, "Checking UA 123 status")
        s = reduce(s, .tick, now: t0 + 91)
        XCTAssertEqual(s.work, .stale)
        XCTAssertEqual(present(s).secondary, "Status unconfirmed")
        XCTAssertEqual(present(s).tone, .warning)
    }

    func testServerStaleFlag() {
        let s = reduce(live(), .work(work(short: "Status unconfirmed", stale: true)), now: t0)
        XCTAssertEqual(s.work, .stale)
    }

    func testApprovalTakesPrecedenceAndClears() {
        let approval = ApprovalInfo(runID: "run_1", requestID: "req_9", description: "Send the email to Dana")
        var s = reduce(live(), .work(work()), now: t0)
        s = reduce(s, .interaction(InteractionSnapshot(status: "waiting_for_approval", runID: "run_1", approval: approval)), now: t0 + 1)
        XCTAssertEqual(s.work, .approval(approval))
        XCTAssertEqual(present(s).secondary, "Needs your approval")
        XCTAssertEqual(present(s).tone, .attention)
        s = reduce(s, .approvalResolved, now: t0 + 2)
        guard case .active = s.work else { return XCTFail("returns to work after approval") }
    }

    func testDoneShowsShortLabelNotTheAnswer() {
        let result = WorkResult(spoken: "UA 123 is on time", full: "United 123 departs EWR at 9:05.\nhttps://united.com",
                                label: "Flight checked")
        let s = reduce(live(), .work(work("completed", result: result)), now: t0)
        XCTAssertEqual(s.work, .done(status: "completed", result: result))
        XCTAssertEqual(present(s).secondary, "Done · Flight checked")
        let unlabeled = WorkResult(spoken: "You're right, my mistake: the Weather Service has rain tomorrow.", full: "x")
        let t = reduce(live(), .work(work("completed", result: unlabeled)), now: t0)
        XCTAssertEqual(present(t).secondary, "Done")
    }

    func testSupersededRunUpdatesAreIgnored() {
        var s = reduce(live(), .work(work(run: "run_1")), now: t0)
        s = reduce(s, .delegationCreated, now: t0 + 1)
        s = reduce(s, .work(work(short: "Old run", run: "run_1")), now: t0 + 2)
        guard case .waiting = s.work else { return XCTFail("old run must not replace new delegation") }
    }

    func testClosedCompleteVsIncomplete() {
        var s = reduce(live(), .endRequested, now: t0)
        XCTAssertEqual(s.connection, .ending)
        let complete = reduce(s, .sessionClosed, now: t0 + 1)
        XCTAssertEqual(complete.connection, .ended(.complete))
        let timedOut = reduce(s, .endTimedOut, now: t0 + 8)
        XCTAssertEqual(timedOut.connection, .ended(.incomplete))
        XCTAssertEqual(present(timedOut).secondary, "Connection dropped")
        XCTAssertEqual(present(timedOut).tone, .plain)
        // A late session.closed after timing out does not rewrite history.
        XCTAssertEqual(reduce(timedOut, .sessionClosed, now: t0 + 9).connection, .ended(.incomplete))
        s = reduce(s, .serverClosed(.incomplete, error: nil), now: t0 + 2)
        XCTAssertEqual(s.connection, .ended(.incomplete))
        // Server finalization confirmation while ending.
        let confirmed = reduce(reduce(live(), .endRequested, now: t0), .interaction(InteractionSnapshot(finalization: "confirmed")), now: t0 + 1)
        XCTAssertEqual(confirmed.connection, .ended(.complete))
    }

    func testEndBeforeAdmissionIsCompleteAndTransportLossIsIncomplete() {
        let early = reduce(reduce(VoiceState(), .startRequested, now: t0), .endRequested, now: t0)
        XCTAssertEqual(early.connection, .ended(.complete))
        let lost = reduce(live(), .transportLost, now: t0)
        XCTAssertEqual(lost.connection, .ended(.incomplete))
    }

    /// SPK-153: how a call ends is told in plain words, never in metering/billing terms.
    func testCallEndWordsArePlainAndNeverAboutUsage() {
        let ending = reduce(live(), .endRequested, now: t0)
        XCTAssertEqual(present(ending).primary, "Ending…")
        XCTAssertEqual(present(ending).secondary, "Hanging up")
        let clean = reduce(ending, .sessionClosed, now: t0 + 1)
        XCTAssertEqual(present(clean).primary, "Call ended")
        XCTAssertEqual(present(clean).secondary, "Call again any time")
        XCTAssertEqual(present(clean).tone, .plain)
        let lost = reduce(live(), .transportLost, now: t0)
        XCTAssertEqual(present(lost).primary, "Call ended")
        XCTAssertEqual(present(lost).secondary, "Connection dropped")
        XCTAssertEqual(present(lost).tone, .plain)
        XCTAssertEqual(lost.lastError, "The call dropped. Your tasks keep running.")
        XCTAssertEqual(lost.lastError, VoiceState.callDropped)
        // The server's own "Voice connection lost…" reads the same to the user.
        let server = reduce(live(), .serverClosed(.incomplete, error: "Voice connection lost: ConnectionResetError"), now: t0)
        XCTAssertEqual(server.lastError, VoiceState.callDropped)
        XCTAssertEqual(present(server).secondary, "Connection dropped")
        // Other server errors pass through unchanged.
        let other = reduce(live(), .serverClosed(.incomplete, error: "The voice service didn't answer."), now: t0)
        XCTAssertEqual(other.lastError, "The voice service didn't answer.")
        for s in [ending, clean, lost, server] {
            let p = present(s)
            for text in [p.primary, p.secondary, s.lastError ?? ""] {
                XCTAssertFalse(text.localizedCaseInsensitiveContains("usage"), text)
                XCTAssertFalse(text.localizedCaseInsensitiveContains("confirm"), text)
            }
        }
    }

    /// A drop while pausing is a pause: no dropped-call banner (ours or the server's wording).
    func testDropWhilePausingLeavesNoDroppedBanner() {
        for event in [VoiceEvent.transportLost, .serverClosed(.incomplete, error: "Voice connection lost.")] {
            var s = reduce(live(), .pauseRequested, now: t0)
            XCTAssertTrue(s.pausing)
            s = reduce(s, event, now: t0 + 1)
            XCTAssertEqual(s.connection, .paused)
            XCTAssertNil(s.lastError)
        }
    }

    func testWorkSurvivesEndOfCall() {
        var s = reduce(live(), .work(work()), now: t0)
        s = reduce(s, .endRequested, now: t0 + 1)
        s = reduce(s, .sessionClosed, now: t0 + 2)
        guard case .active = s.work else { return XCTFail("ending voice does not end work") }
    }

    func testDataChannelParsing() {
        XCTAssertEqual(parseDataChannelMessage(Data(#"{"type":"session.started"}"#.utf8)), [.sessionStarted])
        XCTAssertEqual(parseDataChannelMessage(Data(#"{"type":"session.output_transcript.delta","delta":"Hi"}"#.utf8)), [.outputDelta("Hi")])
        XCTAssertEqual(parseDataChannelMessage(Data(#"{"type":"session.input_transcript.delta","delta":"Yo"}"#.utf8)), [.inputDelta("Yo")])
        XCTAssertEqual(parseDataChannelMessage(Data(#"{"type":"input_transcript.added","item":{"text":"Hello"}}"#.utf8)), [.inputDelta("Hello")])
        XCTAssertEqual(parseDataChannelMessage(Data(#"{"type":"output_transcript.added","item":{"text":"Welcome"}}"#.utf8)), [.outputDelta("Welcome")])
        XCTAssertEqual(parseDataChannelMessage(Data(#"{"type":"delegation.created","item":{"target":"client"}}"#.utf8)), [.delegationCreated])
        XCTAssertEqual(parseDataChannelMessage(Data(#"{"type":"error","error":{"message":"bad"}}"#.utf8)), [.error("bad")])
        XCTAssertEqual(parseDataChannelMessage(Data(#"{"type":"session.closed"}"#.utf8)), [.sessionClosed])
        XCTAssertEqual(parseDataChannelMessage(Data("not json".utf8)), [])
    }

    func testServerStreamParsing() {
        var parser = SSEParser()
        let wire = "id: 1\nevent: snapshot\ndata: {\"interaction\":{\"interaction_id\":\"vi_1\",\"status\":\"working\",\"backend_run_id\":\"run_1\",\"finalization\":\"open\"},\"work\":{\"run_id\":\"run_1\",\"status\":\"working\",\"short_status\":\"Checking UA 123\",\"updated\":1800000000,\"updated_at\":1800000000,\"events\":[]},\"approval\":null}\n\n: ping\n\nid: 2\nevent: closed\ndata: {\"finalization\":\"complete\",\"error\":null}\n\n"
        let events = parser.append(String(wire.prefix(40))) + parser.append(String(wire.dropFirst(40)))
        XCTAssertEqual(events.map(\.name), ["snapshot", "closed"])
        XCTAssertEqual(parser.lastEventID, "2")
        let snapshot = parseServerStreamEvent(events[0])
        XCTAssertEqual(snapshot.count, 3)
        guard case .interaction(let i) = snapshot[0] else { return XCTFail() }
        XCTAssertEqual(i.runID, "run_1")
        guard case .work(let w) = snapshot[1] else { return XCTFail() }
        XCTAssertEqual(w?.shortStatus, "Checking UA 123")
        XCTAssertEqual(snapshot[2], .approval(nil))
        XCTAssertEqual(parseServerStreamEvent(events[1]), [.serverClosed(.complete, error: nil)])
    }

    func testSSEParserIDRetryAndDataPreservation() {
        var parser = SSEParser(lastEventID: "7")
        XCTAssertEqual(parser.lastEventID, "7")
        let events = parser.append("retry: 3000\nid: 8\ndata:  two spaces\ndata:x\n\n")
        XCTAssertEqual(events, [SSEEvent(name: "message", data: " two spaces\nx", id: "8")])
        XCTAssertEqual(parser.retryMilliseconds, 3000)
        XCTAssertEqual(parser.lastEventID, "8")
        XCTAssertEqual(sseReconnectDelay(attempt: 0), 0.5)
        XCTAssertEqual(sseReconnectDelay(attempt: 10), 8)
        XCTAssertEqual(sseReconnectDelay(attempt: 0, serverRetryMilliseconds: 3000), 3)
    }

    func testSessionAdmissionAndBodyPreserveSDPBytes() throws {
        let sdp = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\n"
        let body = try sessionRequestBody(sdp: sdp)
        let decoded = try JSONSerialization.jsonObject(with: body) as? [String: String]
        XCTAssertEqual(decoded, ["sdp": sdp], "trailing CRLF must survive")
        let legacy = SessionAdmission(json: ["interaction_id": "vi_1", "transport": ["type": "webrtc", "sdp": "v=0\r\n"]])
        XCTAssertEqual(legacy?.answerSDP, "v=0\r\n")
        let v2 = SessionAdmission(json: ["interaction_id": "vi_2", "sdp": "v=0\r\na\r\n"])
        XCTAssertEqual(v2?.interactionID, "vi_2")
        XCTAssertNil(SessionAdmission(json: ["interaction_id": "vi_3"]))
    }

    func testStatusDwell() {
        var dwell = StatusDwell(minimumDwell: 1.5)
        XCTAssertTrue(dwell.offer("A", now: t0))
        XCTAssertFalse(dwell.offer("B", now: t0 + 0.5))
        XCTAssertEqual(dwell.shown, "A")
        XCTAssertFalse(dwell.tick(now: t0 + 1.0))
        XCTAssertTrue(dwell.tick(now: t0 + 1.6))
        XCTAssertEqual(dwell.shown, "B")
        XCTAssertTrue(dwell.offer("Needs your approval", urgent: true, now: t0 + 1.7))
        XCTAssertEqual(dwell.shown, "Needs your approval")
    }

    func testElapsedFormatting() {
        XCTAssertEqual(formatElapsed(5), "5s")
        XCTAssertEqual(formatElapsed(92), "1m 32s")
        XCTAssertEqual(formatElapsed(3720), "1h 02m")
    }

    func testQuietPauseSaysWhyAndClearsOnResume() {
        XCTAssertEqual(VoiceState.defaultIdleTimeout, 15 * 60)
        var s = live()
        s = reduce(s, .quietPauseRequested(minutes: 15), now: t0 + 1)
        XCTAssertTrue(s.pausing)
        s = reduce(s, .sessionClosed, now: t0 + 2)
        XCTAssertEqual(s.connection, .paused)
        XCTAssertEqual(s.quietPausedMinutes, 15)
        XCTAssertEqual(present(s).secondary, "Paused after 15 min of quiet · your jobs keep running")
        XCTAssertEqual(present(s).pauseLabel, "Resume")
        s = reduce(s, .resumeRequested, now: t0 + 3)
        XCTAssertNil(s.quietPausedMinutes)
        // The user's own Pause keeps the usual line.
        var p = live()
        p = reduce(p, .pauseRequested, now: t0 + 1)
        p = reduce(p, .sessionClosed, now: t0 + 2)
        XCTAssertNil(p.quietPausedMinutes)
        XCTAssertEqual(present(p).secondary, "Voice off · not billed · Resume to continue")
    }

    func testCallLimitChoices() {
        XCTAssertEqual(ServerSettings.choices(ServerSettings.idlePauseChoices, including: 15), [0, 5, 10, 15, 30, 60])
        XCTAssertEqual(ServerSettings.choices(ServerSettings.maxCallChoices, including: 30), [15, 30, 60, 120, 240, 0])
        XCTAssertEqual(ServerSettings.choices(ServerSettings.maxCallChoices, including: 45, noLimit: false), [15, 30, 45, 60, 120, 240])
        XCTAssertEqual(ServerSettings.idlePauseLabel(0), "Never")
        XCTAssertEqual(ServerSettings.idlePauseLabel(10), "After 10 min")
        XCTAssertEqual(ServerSettings.maxCallLabel(0), "No limit")
        XCTAssertEqual(ServerSettings.maxCallLabel(120), "2 hours")
        XCTAssertTrue(ServerSettings.callLimitsFooter(idlePauseMinutes: 15, maxCallMinutes: 0).contains("voice provider's minutes"))
        XCTAssertTrue(ServerSettings.callLimitsFooter(idlePauseMinutes: 15, maxCallMinutes: 30).contains("jobs keep running"))
        let caps = try? JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"call_no_limit":true}"#.utf8))
        XCTAssertEqual(caps?.callNoLimit, true)
    }
}
