import XCTest
@testable import SpeakeasyCore

final class SessionHistoryTests: XCTestCase {
    private func json(_ s: String) -> Any { try! JSONSerialization.jsonObject(with: Data(s.utf8)) }

    func testAReplyCarriesItsPictures() {
        // SPK-214: a reply's MEDIA files arrive as Files rows (ids, never paths); a picture-only reply still shows.
        let e = HistoryEntry(json: json(#"{"id":"m2","kind":"assistant","text":"","files":[{"id":"abcdefghijklmnopqrstuvwxyz","name":"home-mockup.png","kind":"image","mime":"image/png","bytes":2048,"at":1794000000,"session_id":"d1"}]}"#))
        XCTAssertEqual(e?.files.map(\.name), ["home-mockup.png"])
        XCTAssertEqual(e?.files.first?.kind, .image)
        XCTAssertNil(HistoryEntry(json: json(#"{"id":"m3","kind":"assistant","text":""}"#)), "an empty reply with nothing to show stays out")
        let user = HistoryEntry(json: json(#"{"id":"m4","kind":"user","text":"hi","files":[{"id":"x","name":"a.png"}]}"#))
        XCTAssertEqual(user?.files.count, 0, "only replies carry files")
    }

    func testCapabilityFlag() throws {
        let on = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"history":true}"#.utf8))
        XCTAssertTrue(on.history)
        let old = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"task_messages":true}"#.utf8))
        XCTAssertFalse(old.history, "an older plugin hides the side panel")
    }

    func testReplyCarriesItsPictures() {
        let withPicture = HistoryEntry(json: json(#"""
        {"id": "m1", "kind": "assistant", "text": "Here is the chart.",
         "files": [{"id": "abc123", "name": "chart.png", "kind": "image", "mime": "image/png", "bytes": 2048, "at": 1793891284, "session_id": "d1"}]}
        """#))
        XCTAssertEqual(withPicture?.files.map(\.id), ["abc123"])
        XCTAssertEqual(withPicture?.files.first?.kind, .image)
        let onlyPicture = HistoryEntry(json: json(#"""
        {"id": "m2", "kind": "assistant", "text": "",
         "files": [{"id": "def456", "name": "sketch.png", "kind": "image", "mime": "image/png", "bytes": 10, "at": 1793891285, "session_id": "d1"}]}
        """#))
        XCTAssertNotNil(onlyPicture, "a reply that was only a picture still shows")
        XCTAssertNil(HistoryEntry(json: json(#"{"id": "m3", "kind": "assistant", "text": ""}"#)))
        var chat = HistoryConversation(id: "d1", title: "Porch", source: "discord", sourceLabel: "Discord")
        chat.entries = [withPicture!, onlyPicture!]
        XCTAssertEqual(HistoryThread(chat).messages.map { $0.files.count }, [1, 1])
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
        XCTAssertNil(page.sources, "older plugins send no sources facet")
        let faceted = HistoryPage(json: json(#"{"sessions": [], "sources": [{"source": "discord", "count": 3}, {"count": 1}]}"#))
        XCTAssertEqual(faceted.sources, [HistorySourceCount(source: "discord", count: 3)])
    }

    func testSourceFiltersOnlyShowSourcesThePersonHas() {
        let all = HistoryList.defaultFilters
        // The facet: Slack has nothing, so it isn't offered.
        let facet = [HistorySourceCount(source: "speakeasy", count: 4), HistorySourceCount(source: "discord", count: 2),
                     HistorySourceCount(source: "slack", count: 0)]
        XCTAssertEqual(HistorySources.shown(choices: all, sources: facet, seen: [], selected: nil).map(\.id), ["speakeasy", "discord"])
        // Older plugin: derived from the loaded rows (cli → terminal).
        let rows = [HistorySession(id: "a", title: "A", source: "cli", sourceLabel: "Terminal", lastActivity: Date(), preview: ""),
                    HistorySession(id: "b", title: "B", source: "telegram", sourceLabel: "Telegram", lastActivity: Date(), preview: "")]
        XCTAssertEqual(HistorySources.shown(choices: all, sources: nil, seen: HistorySources.seen(in: rows), selected: nil).map(\.id),
                       ["telegram", "terminal"])
        // One source: no picker; a chosen filter always stays (so it can be cleared).
        let one = HistorySources.shown(choices: all, sources: [HistorySourceCount(source: "speakeasy", count: 9)], seen: [], selected: nil)
        XCTAssertFalse(HistorySources.showsPicker(one, selected: nil))
        XCTAssertTrue(HistorySources.showsPicker(all, selected: nil))
        let kept = HistorySources.shown(choices: all, sources: [HistorySourceCount(source: "speakeasy", count: 9)], seen: [], selected: "discord")
        XCTAssertEqual(kept.map(\.id), ["speakeasy", "discord"])
        XCTAssertTrue(HistorySources.showsPicker(kept, selected: "discord"))
    }

    func testPagePathEncodesQueryCursorAndFilter() {
        XCTAssertEqual(HistoryPage.path(), "/voice/history?hide=kanban,scheduled", "SPK-263: workers hidden by default")
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
         "state": "running", "send_via": "linked", "has_more": true, "before": 120,
         "entries": [
           {"kind": "user", "id": "m1", "at": 1793891100, "text": "Size the brackets"},
           {"kind": "tools", "id": "t2", "items": [{"n": 1, "text": "Ran a command", "tool": "terminal", "status": "done",
              "args": "ls", "result": "ok", "started_at": 1793891101, "ended_at": 1793891102}]},
           {"kind": "tools", "id": "t3", "items": []},
           {"kind": "note", "id": "m4", "text": "A helper reported back"},
           {"kind": "assistant", "id": "m5", "text": "Three brackets."},
           {"kind": "weird", "id": "m6", "text": "?"}],
         "jobs": [{"run_id": "run_1", "status": "waiting_for_approval", "title": "Order brackets", "session_id": "d1"}],
         "approvals": [{"run_id": "run_1", "description": "Order 3?", "choices": ["once", "always", "deny"]}]}}
        """#))!
        XCTAssertEqual(c.entries.map(\.kind), [.user, .tools, .note, .assistant])
        XCTAssertEqual(c.entries[1].tools.first?.tool, "terminal")
        XCTAssertEqual(c.entries[1].tools.first?.args, "ls")
        XCTAssertEqual(c.sendVia, .linked)
        XCTAssertTrue(c.hasMore)
        XCTAssertEqual(c.before, 120)
        XCTAssertEqual(c.jobs.first?.runID, "run_1")
        XCTAssertEqual(c.jobs.first?.sessionID, "d1")
        XCTAssertEqual(c.approvals.first?.choices, ["once", "deny"], "never always")
        XCTAssertEqual(HistoryApproval.label("once"), "Approve")
        XCTAssertTrue(c.isWorking)
        XCTAssertNil(HistoryConversation(json: json(#"{"error": "x"}"#)))
    }

    func testMessageRequestAndReply() {
        XCTAssertNil(HistoryMessageRequest(sessionID: "d1", text: "   "))
        let r = HistoryMessageRequest(sessionID: "2026 a/b", text: " Order them ", source: .voiceMessage, clientID: "c1")!
        XCTAssertEqual(r.path, "/voice/history/2026%20a%2Fb/message")
        XCTAssertEqual(r.body["text"] as? String, "Order them")
        XCTAssertEqual(r.body["source"] as? String, "voice_message")
        let task = HistoryMessageReply(json: json(#"{"ok": true, "via": "task", "run_id": "run_9"}"#))
        XCTAssertEqual(task, HistoryMessageReply(via: .task, runID: "run_9"))
        let linked = HistoryMessageReply(json: json(#"{"ok": true, "via": "linked", "run_id": "run_3"}"#))
        XCTAssertEqual(linked?.via, .linked)
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

    func testAVoiceMessageEntryDecodesAsVoice() {
        let spoken = HistoryEntry(json: ["id": "m1", "kind": "user", "text": "Make it oak", "voice": true])
        let typed = HistoryEntry(json: ["id": "m2", "kind": "user", "text": "Add a hook rail"])
        XCTAssertEqual(spoken?.voice, true)
        XCTAssertEqual(typed?.voice, false)
    }
}

final class VoiceNoteFlowTests: XCTestCase {
    let t0 = Date(timeIntervalSince1970: 1000)

    func testStopSendsTheClipAndNothingGoesInTheField() {
        var f = VoiceMessageFlow(permitted: true, sendsOnStop: true)
        XCTAssertEqual(f.handle(.micTapped(now: t0), draft: "half typed"), .startRecording)
        f.handle(.partial("book the table"))
        XCTAssertEqual(f.fieldText, "half typed", "no on-device words in the field")
        XCTAssertEqual(f.handle(.micTapped(now: t0)), .sendClip)
        XCTAssertEqual(f.phase, .idle)
    }

    func testAPauseNeverEndsTheRecording() {
        // Speech, a pause (the recognizer ends its segment and says final), more speech, stop.
        var f = VoiceMessageFlow(permitted: true, sendsOnStop: true)
        f.handle(.micTapped(now: t0))
        XCTAssertEqual(f.handle(.final("Book the table for four.")), .none)
        XCTAssertTrue(f.isRecording, "the recording goes on through the pause")
        XCTAssertEqual(f.handle(.failed("no speech")), .cancelRecording, "a real mic failure still stops")
        var g = VoiceMessageFlow(permitted: true, sendsOnStop: true)
        g.handle(.micTapped(now: t0))
        g.handle(.final("Book the table for four."))
        g.handle(.partial("Then text everyone"))
        XCTAssertEqual(g.handle(.micTapped(now: t0)), .sendClip, "one clip, both halves")
    }

    func testHermesTranscribingKeepsRecordingThroughAPause() {
        var f = VoiceMessageFlow(permitted: true, wholeClip: true)
        f.handle(.micTapped(now: t0))
        XCTAssertEqual(f.handle(.final("Book the table for four.")), .none)
        XCTAssertTrue(f.isRecording)
        XCTAssertEqual(f.handle(.micTapped(now: t0)), .stopRecording)
    }

    func testNoOnDeviceRecognizerIsFineForVoiceNotes() {
        var f = VoiceMessageFlow(sendsOnStop: true)
        f.handle(.micTapped(now: t0))
        XCTAssertEqual(f.handle(.permissions(granted: true, onDevice: false)), .startRecording)
    }

    func testRequestCarriesTheMessageAsHeaders() {
        let note = VoiceNoteRequest(target: .session("api_shelf"), audio: Data([1, 2, 3]), clientID: "vm-1",
                                    duration: 31.42, mode: .steer, answers: "run_9")
        XCTAssertEqual(note.path, "/voice/history/api_shelf/voice-message")
        XCTAssertEqual(note.headers["X-Speakeasy-Client-Id"], "vm-1")
        XCTAssertEqual(note.headers["X-Speakeasy-Duration"], "31.4")
        XCTAssertEqual(note.headers["X-Speakeasy-Mode"], "steer")
        XCTAssertEqual(note.headers["X-Speakeasy-Answers"], "run_9")
        XCTAssertEqual(note.headers["Content-Type"], "audio/mp4")
        let task = VoiceNoteRequest(target: .task("run_5"), audio: Data(), duration: 2, answers: "run_9")
        XCTAssertEqual(task.path, "/voice/work/run_5/voice-message")
        XCTAssertNil(task.headers["X-Speakeasy-Answers"])
    }

    /// Home's composer (SPK-198): a recording with no conversation yet starts a new job and chat.
    func testANewJobVoiceNoteStartsItsOwnChat() throws {
        let note = VoiceNoteRequest(target: .newJob, audio: Data([1]), clientID: "vm-home", duration: 6.5,
                                    mode: .steer, answers: "run_9", attachments: ["att_1", "att_2"])
        XCTAssertEqual(note.path, "/voice/work/voice-message")
        XCTAssertNil(note.headers["X-Speakeasy-Mode"])      // nothing runs in a chat that doesn't exist yet
        XCTAssertNil(note.headers["X-Speakeasy-Answers"])
        XCTAssertEqual(note.headers["X-Speakeasy-Attachments"], "att_1,att_2")
        let reply = try XCTUnwrap(VoiceNoteReply(json: ["ok": true, "run_id": "r2", "delivered_as": "new_run",
                                                        "session_id": "speakeasy_task_1",
                                                        "voice": ["transcript": "Find a quiet cafe", "duration": 6.5]]))
        XCTAssertEqual(reply.sessionID, "speakeasy_task_1")
        XCTAssertEqual(reply.runID, "r2")
        XCTAssertEqual(reply.deliveredAs, .newRun)
    }

    func testReplyAndPageEntries() throws {
        let reply = VoiceNoteReply(json: ["ok": true, "via": "session_job", "run_id": "r1", "delivered_as": "steer",
                                          "voice": ["transcript": " Order the cedar boards ", "duration": 4.0]])
        XCTAssertEqual(reply?.transcript, "Order the cedar boards")
        XCTAssertEqual(reply?.deliveredAs, .steer)
        XCTAssertNil(VoiceNoteReply(json: ["ok": true, "voice": ["transcript": NSNull()]])?.transcript)
        let entry = try XCTUnwrap(HistoryEntry(json: ["id": "m1", "kind": "user", "text": "Order the cedar boards", "voice": true,
                                                      "voice_clip": "vm-1", "voice_duration": 4.5]))
        XCTAssertEqual(entry.voiceClip, "vm-1")
        XCTAssertEqual(entry.voiceDuration, 4.5)
        // Hermes couldn't write it down: still a voice message on the page.
        XCTAssertNotNil(HistoryEntry(json: ["id": "m2", "kind": "user", "text": "", "voice": true]))
        XCTAssertNil(HistoryEntry(json: ["id": "m3", "kind": "user", "text": ""]))
    }
}
