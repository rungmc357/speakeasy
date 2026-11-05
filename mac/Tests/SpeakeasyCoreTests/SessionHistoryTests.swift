import XCTest
@testable import SpeakeasyCore

final class SessionHistoryTests: XCTestCase {
    private func json(_ s: String) -> Any { try! JSONSerialization.jsonObject(with: Data(s.utf8)) }

    func testCapabilityFlag() throws {
        let on = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"history":true}"#.utf8))
        XCTAssertTrue(on.history)
        let old = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"task_messages":true}"#.utf8))
        XCTAssertFalse(old.history, "an older plugin hides the side panel")
    }

    func testPageDecodes() {
        let page = HistoryPage(json: json(#"""
        {"sessions": [
          {"id": "d1", "title": "Shelf brackets", "source": "discord", "source_label": "Discord · #build",
           "last_activity": 1793891284.5, "preview": "Three brackets.", "state": "running", "match": "walnut"},
          {"id": "speakeasy_task_1", "title": "Bake sale flyer", "source": "speakeasy", "source_label": "Speakeasy",
           "last_activity": 1793890001, "state": "needs_you", "run_ids": ["run_2"]},
          {"title": "no id"}],
         "next_cursor": "1793890001.0|speakeasy_task_1",
         "filters": [{"id": "speakeasy", "label": "Speakeasy"}, {"id": "discord", "label": "Discord"}]}
        """#))
        XCTAssertEqual(page.sessions.map(\.id), ["d1", "speakeasy_task_1"])
        XCTAssertEqual(page.sessions[0].sourceLabel, "Discord · #build")
        XCTAssertEqual(page.sessions[0].state, .running)
        XCTAssertEqual(page.sessions[0].match, "walnut")
        XCTAssertTrue(page.sessions[1].isSpeakeasy)
        XCTAssertEqual(page.sessions[1].state, .needsYou)
        XCTAssertEqual(page.sessions[1].runIDs, ["run_2"])
        XCTAssertEqual(page.nextCursor, "1793890001.0|speakeasy_task_1")
        XCTAssertEqual(page.filters.map(\.id), ["speakeasy", "discord"])
    }

    func testPagePathEncodesQueryCursorAndFilter() {
        XCTAssertEqual(HistoryPage.path(), "/voice/history")
        let path = HistoryPage.path(query: "  tomato beds ", source: "discord", cursor: "1793.5|a+b")
        XCTAssertTrue(path.hasPrefix("/voice/history?"))
        XCTAssertTrue(path.contains("q=tomato%20beds"))
        XCTAssertTrue(path.contains("source=discord"))
        XCTAssertTrue(path.contains("cursor=1793.5%7Ca%2Bb") || path.contains("cursor=1793.5|a%2Bb"))
    }

    func testGroupsTodayYesterdayEarlier() {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        let now = Date(timeIntervalSince1970: 1_793_880_000) // midday UTC
        let rows = [
            HistorySession(id: "old", title: "", source: "cli", sourceLabel: "Terminal", lastActivity: now.addingTimeInterval(-5 * 86400)),
            HistorySession(id: "today2", title: "", source: "cli", sourceLabel: "Terminal", lastActivity: now.addingTimeInterval(-60)),
            HistorySession(id: "yday", title: "", source: "cli", sourceLabel: "Terminal", lastActivity: now.addingTimeInterval(-86400)),
            HistorySession(id: "today1", title: "", source: "cli", sourceLabel: "Terminal", lastActivity: now),
            HistorySession(id: "nodate", title: "", source: "cli", sourceLabel: "Terminal", lastActivity: nil),
        ]
        let groups = HistoryGroup.grouped(rows, now: now, calendar: cal)
        XCTAssertEqual(groups.map(\.0), [.today, .yesterday, .earlier])
        XCTAssertEqual(groups[0].1.map(\.id), ["today1", "today2"])
        XCTAssertEqual(groups[2].1.map(\.id), ["old", "nodate"])
    }

    func testConversationDecodesEntriesToolsJobsAndSends() {
        let c = HistoryConversation(json: json(#"""
        {"session": {"id": "d1", "title": "Shelf brackets", "source": "discord", "source_label": "Discord · #build",
         "state": "running", "send_via": "hermes", "has_more": true, "before": 120,
         "entries": [
           {"kind": "user", "id": "m1", "at": 1793891100, "text": "Size the brackets"},
           {"kind": "tools", "id": "t2", "items": [{"n": 1, "text": "Ran a command", "tool": "terminal", "status": "done",
              "args": "ls", "result": "ok", "started_at": 1793891101, "ended_at": 1793891102}]},
           {"kind": "tools", "id": "t3", "items": []},
           {"kind": "note", "id": "m4", "text": "A helper reported back"},
           {"kind": "assistant", "id": "m5", "text": "Three brackets."},
           {"kind": "weird", "id": "m6", "text": "?"}],
         "jobs": [{"run_id": "run_1", "status": "waiting_for_approval", "title": "Order brackets"}],
         "sends": [{"send_id": "hs_1", "status": "needs_you", "text": "Order them",
                    "prompt": {"kind": "approval", "prompt_id": "p1", "text": "Order 3?", "choices": ["once", "deny"]}},
                   {"send_id": "hs_0", "status": "completed", "text": "hi", "reply": "Hello"}]}}
        """#))!
        XCTAssertEqual(c.entries.map(\.kind), [.user, .tools, .note, .assistant])
        XCTAssertEqual(c.entries[1].tools.first?.tool, "terminal")
        XCTAssertEqual(c.entries[1].tools.first?.args, "ls")
        XCTAssertEqual(c.sendVia, .hermes)
        XCTAssertTrue(c.hasMore)
        XCTAssertEqual(c.before, 120)
        XCTAssertEqual(c.jobs.first?.runID, "run_1")
        XCTAssertEqual(c.waitingJobs.map(\.runID), ["run_1"])
        XCTAssertEqual(c.sends.first?.prompt?.choices, ["once", "deny"])
        XCTAssertTrue(c.isWorking)
        XCTAssertNil(HistoryConversation(json: json(#"{"error": "x"}"#)))
    }

    func testMessageRequestAndReply() {
        XCTAssertNil(HistoryMessageRequest(sessionID: "d1", text: "   "))
        let r = HistoryMessageRequest(sessionID: "2026 a/b", text: " Order them ", source: .voiceMessage, clientID: "c1")!
        XCTAssertEqual(r.path, "/voice/history/2026%20a%2Fb/message")
        XCTAssertEqual(r.body["text"] as? String, "Order them")
        XCTAssertEqual(r.body["source"] as? String, "voice_message")
        let task = HistoryMessageReply(json: json(#"{"ok": true, "via": "task", "run_id": "run_9", "send_id": null}"#))
        XCTAssertEqual(task, HistoryMessageReply(via: .task, runID: "run_9", sendID: nil))
        let hermes = HistoryMessageReply(json: json(#"{"ok": true, "via": "hermes", "run_id": null, "send_id": "hs_1"}"#))
        XCTAssertEqual(hermes?.sendID, "hs_1")
        XCTAssertNil(HistoryMessageReply(json: json(#"{"ok": false}"#)))
    }
}

final class VoiceMessageFlowTests: XCTestCase {
    let t0 = Date(timeIntervalSince1970: 1000)

    func testFirstTapAsksThenRecordsThenReviews() {
        var f = VoiceMessageFlow()
        XCTAssertEqual(f.handle(.micTapped(now: t0)), .askPermissions)
        XCTAssertEqual(f.phase, .asking)
        XCTAssertEqual(f.handle(.permissions(granted: true, onDevice: true)), .startRecording)
        XCTAssertTrue(f.isRecording)
        f.handle(.partial("order the"))
        XCTAssertEqual(f.fieldText, "order the")
        XCTAssertEqual(f.handle(.micTapped(now: t0)), .stopRecording)
        XCTAssertEqual(f.phase, .transcribing)
        f.handle(.final("Order the walnut brackets"))
        XCTAssertEqual(f.phase, .reviewing)
        XCTAssertEqual(f.fieldText, "Order the walnut brackets")
        f.handle(.sent)
        XCTAssertEqual(f.phase, .idle)
        XCTAssertTrue(f.permitted)
        // Second time: no prompt.
        XCTAssertEqual(f.handle(.micTapped(now: t0)), .startRecording)
    }

    func testTranscriptAppendsToADraft() {
        var f = VoiceMessageFlow(permitted: true)
        f.handle(.micTapped(now: t0), draft: "Also,")
        f.handle(.micTapped(now: t0))
        f.handle(.final("make it three"))
        XCTAssertEqual(f.fieldText, "Also, make it three")
    }

    func testDeniedAndNoOnDeviceSayItPlainly() {
        var f = VoiceMessageFlow()
        f.handle(.micTapped(now: t0))
        f.handle(.permissions(granted: false, onDevice: true))
        XCTAssertEqual(f.unavailableMessage, VoiceMessageFlow.deniedText)
        var g = VoiceMessageFlow()
        g.handle(.micTapped(now: t0))
        g.handle(.permissions(granted: true, onDevice: false))
        XCTAssertEqual(g.unavailableMessage, VoiceMessageFlow.noOnDeviceText)
        XCTAssertFalse(g.permitted)
    }

    func testCancelWhileRecordingStopsTheMicAndDropsTheWords() {
        var f = VoiceMessageFlow(permitted: true)
        f.handle(.micTapped(now: t0))
        f.handle(.partial("never mind"))
        XCTAssertEqual(f.handle(.cancel), .cancelRecording)
        XCTAssertEqual(f.phase, .idle)
        XCTAssertEqual(f.fieldText, "")
        XCTAssertEqual(f.handle(.cancel), .none)
    }

    func testNothingHeardAndRecognizerEndingOnItsOwn() {
        var f = VoiceMessageFlow(permitted: true)
        f.handle(.micTapped(now: t0))
        XCTAssertEqual(f.handle(.final("hello there")), .stopRecording, "the recognizer ended first: stop the mic")
        XCTAssertEqual(f.phase, .reviewing)
        var g = VoiceMessageFlow(permitted: true)
        g.handle(.micTapped(now: t0))
        g.handle(.micTapped(now: t0))
        g.handle(.final("  "))
        XCTAssertEqual(g.unavailableMessage, VoiceMessageFlow.nothingHeardText)
    }

    func testTimerAndCap() {
        var f = VoiceMessageFlow(permitted: true)
        f.handle(.micTapped(now: t0))
        XCTAssertEqual(f.elapsed(now: t0.addingTimeInterval(7.6)) ?? 0, 7.6, accuracy: 0.01)
        XCTAssertEqual(VoiceMessageFlow.clock(67.9), "1:07")
        XCTAssertFalse(f.shouldAutoStop(now: t0.addingTimeInterval(60)))
        XCTAssertTrue(f.shouldAutoStop(now: t0.addingTimeInterval(VoiceMessageFlow.maxSeconds)))
    }
}
