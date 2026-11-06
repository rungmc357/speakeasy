import XCTest
@testable import SpeakeasyCore

/// The Mac history window's state (HistoryBrowsing.swift) on top of the shared history models.
final class HistoryBrowsingTests: XCTestCase {
    private func row(_ id: String, _ title: String = "T", at: Date? = nil, runs: [String] = []) -> HistorySession {
        HistorySession(id: id, title: title, source: "speakeasy", sourceLabel: "Speakeasy", lastActivity: at, runIDs: runs)
    }

    // MARK: List paging

    func testPagingAppendsDedupesAndEnds() throws {
        var list = HistoryList()
        let q1 = list.reset()
        XCTAssertNil(q1.cursor)
        XCTAssertEqual(q1.path, "/voice/history?limit=40")
        XCTAssertTrue(list.loading)
        XCTAssertNil(list.loadMore(), "one page at a time")
        XCTAssertTrue(list.apply(HistoryPage(sessions: [row("a"), row("b")], nextCursor: "1793890001.2|b"), for: q1))
        let q2 = try XCTUnwrap(list.loadMore())
        XCTAssertEqual(q2.cursor, "1793890001.2|b")
        XCTAssertEqual(q2.path, "/voice/history?cursor=1793890001.2%7Cb&limit=40")
        // b again (it moved while paging): updated in place, never repeated.
        list.apply(HistoryPage(sessions: [row("b", "B2"), row("c")], nextCursor: nil), for: q2)
        XCTAssertEqual(list.sessions.map(\.id), ["a", "b", "c"])
        XCTAssertEqual(list.sessions[1].title, "B2")
        XCTAssertTrue(list.reachedEnd)
        XCTAssertNil(list.loadMore(), "no cursor: the end")
    }

    func testARepeatedRowNeverTrapsAndShowsOnce() throws {
        var list = HistoryList()
        let q1 = list.reset()
        // A page that lists the same session twice, then a live refresh that repeats one, then the next page.
        list.apply(HistoryPage(sessions: [row("a"), row("a", "A2"), row("b")], nextCursor: "c1"), for: q1)
        XCTAssertEqual(list.sessions.map(\.id), ["a", "b"])
        list.merge(HistoryPage(sessions: [row("b"), row("b"), row("a")], nextCursor: "c1"), for: q1)
        XCTAssertEqual(list.sessions.map(\.id), ["b", "a"])
        let q2 = try XCTUnwrap(list.loadMore())
        list.apply(HistoryPage(sessions: [row("c"), row("a")], nextCursor: nil), for: q2)
        XCTAssertEqual(list.sessions.map(\.id), ["b", "a", "c"])
    }

    func testPagingErrorStopsAutoPagingUntilRetry() throws {
        var list = HistoryList()
        let q = list.reset()
        list.fail("Couldn't reach your Hermes.", for: q)
        XCTAssertEqual(list.error, "Couldn't reach your Hermes.")
        XCTAssertNil(list.loadMore())
        let retry = try XCTUnwrap(list.retry())
        XCTAssertNil(list.error)
        XCTAssertNil(retry.cursor)
    }

    func testUpsertMovesToTopAndGroups() {
        var cal = Calendar(identifier: .gregorian); cal.timeZone = TimeZone(identifier: "America/New_York")!
        let now = cal.date(from: DateComponents(year: 2026, month: 11, day: 5, hour: 9))!
        var list = HistoryList()
        let q = list.reset()
        list.apply(HistoryPage(sessions: [
            row("y", at: cal.date(from: DateComponents(year: 2026, month: 11, day: 4, hour: 23))),
            row("e", at: cal.date(from: DateComponents(year: 2026, month: 10, day: 30)))], nextCursor: nil), for: q)
        list.upsert(row("t", at: now))
        XCTAssertEqual(list.sessions.map(\.id), ["t", "y", "e"])
        XCTAssertEqual(list.grouped(now: now, calendar: cal).map(\.0), [.today, .yesterday, .earlier])
    }

    // MARK: Search + filter

    func testSearchStartsOverAndDropsStaleReplies() throws {
        var list = HistoryList()
        let first = list.reset()
        list.apply(HistoryPage(sessions: [row("a")], nextCursor: "c"), for: first)
        let search = try XCTUnwrap(list.setQuery("walnut"))
        XCTAssertEqual(search.q, "walnut")
        XCTAssertNil(search.cursor, "a new search starts at the top")
        XCTAssertTrue(list.sessions.isEmpty)
        XCTAssertTrue(list.isFiltered)
        XCTAssertNil(list.setQuery("walnut "), "same search: no request")
        let narrower = try XCTUnwrap(list.setQuery("walnut shelf"))
        XCTAssertFalse(list.apply(HistoryPage(sessions: [row("old")]), for: search), "the older search's reply is dropped")
        XCTAssertFalse(list.fail("x", for: search))
        XCTAssertTrue(list.apply(HistoryPage(sessions: [row("hit")]), for: narrower))
        XCTAssertEqual(list.sessions.map(\.id), ["hit"])
        let cleared = try XCTUnwrap(list.setQuery("  "))
        XCTAssertEqual(cleared.q, "")
        XCTAssertFalse(list.isFiltered)
    }

    func testSourceFilterAndQueryPath() throws {
        var list = HistoryList()
        _ = list.reset()
        let q = try XCTUnwrap(list.setSource("discord"))
        XCTAssertEqual(q.source, "discord")
        XCTAssertNil(list.setSource("discord"))
        let both = try XCTUnwrap(list.setQuery("a+b & c"))
        XCTAssertEqual(both.path, "/voice/history?q=a%2Bb%20%26%20c&source=discord&limit=40")
        XCTAssertEqual(list.filterChoices, HistoryList.defaultFilters, "until the server lists its own")
        list.apply(HistoryPage(sessions: [], filters: [HistoryFilter(id: "gui", label: "Desktop")]), for: both)
        XCTAssertEqual(list.filterChoices.map(\.label), ["Desktop"])
    }

    func testListOffersOnlySourcesWithSessions() throws {
        var list = HistoryList()
        let q = list.reset()
        let row = HistorySession(id: "d1", title: "D", source: "discord", sourceLabel: "Discord", lastActivity: Date(), preview: "")
        // Older plugin (no facet): the loaded rows' sources; one source → no picker.
        list.apply(HistoryPage(sessions: [row], filters: HistoryList.defaultFilters), for: q)
        XCTAssertEqual(list.shownFilters.map(\.id), ["discord"])
        XCTAssertFalse(list.showsSourcePicker)
        // The facet wins over the loaded rows (it counts every page).
        let q2 = list.reset()
        list.apply(HistoryPage(sessions: [row], filters: HistoryList.defaultFilters,
                               sources: [HistorySourceCount(source: "speakeasy", count: 2), HistorySourceCount(source: "discord", count: 1)]), for: q2)
        XCTAssertEqual(list.shownFilters.map(\.id), ["speakeasy", "discord"])
        XCTAssertTrue(list.showsSourcePicker)
    }

    // MARK: Send by id

    func testSessionPageSendsToThatSessionByID() {
        guard case .message(let r)? = HistoryTarget.session("2026/11 a").outgoing("  Order them please  ", source: .typed, clientID: "c1") else {
            return XCTFail("a session page sends a message to that session")
        }
        XCTAssertEqual(r.sessionID, "2026/11 a")
        XCTAssertEqual(r.path, "/voice/history/2026%2F11%20a/message", "by id, escaped: never the router")
        XCTAssertEqual(r.body["text"] as? String, "Order them please")
        XCTAssertEqual(r.body["client_id"] as? String, "c1")
        guard case .message(let v)? = HistoryTarget.session("s").outgoing("hi", source: .voiceMessage) else { return XCTFail() }
        XCTAssertEqual(v.body["source"] as? String, "voice_message")
        XCTAssertNil(HistoryTarget.session("s").outgoing("   ", source: .typed), "nothing to send")
    }

    func testNewChatStartsWork() {
        guard case .newChat(let text, _)? = HistoryTarget.newChat.outgoing(" Plan the offsite ", source: .typed) else {
            return XCTFail("New chat goes to POST /voice/work")
        }
        XCTAssertEqual(text, "Plan the offsite")
        XCTAssertNil(HistoryTarget.newChat.sessionID)
    }

    func testAttachmentsRideOnASessionMessageAndMayStandAlone() {
        guard case .message(let r)? = HistoryTarget.session("s").outgoing("  ", source: .typed, clientID: "c1",
                                                                         attachments: ["att_1", "att_2"]) else {
            return XCTFail("attachments alone are something to send")
        }
        XCTAssertEqual(r.body["attachments"] as? [String], ["att_1", "att_2"])
        XCTAssertEqual(r.body["text"] as? String, "")
        guard case .message(let plain)? = HistoryTarget.session("s").outgoing("hi", source: .voiceMessage) else { return XCTFail() }
        XCTAssertNil(plain.body["attachments"], "no key without attachments (older servers refuse it)")
        guard case .newChat(let text, _)? = HistoryTarget.newChat.outgoing("", source: .typed, attachments: ["att_1"]) else {
            return XCTFail("a New chat can start from a photo alone")
        }
        XCTAssertEqual(text, "")
        XCTAssertNil(HistoryTarget.newChat.outgoing(" ", source: .typed))
    }

    func testTwoVoiceNotesWithTheSameWordsEachKeepTheirOwnRecordingAfterAReload() throws {
        let t0 = Date(timeIntervalSince1970: 1_000_000)
        var box = HistoryOutbox()
        let first = VoiceNoteRequest(target: .session("s"), audio: Data([1]), clientID: "vm-a", duration: 2)
        let second = VoiceNoteRequest(target: .session("s"), audio: Data([2]), clientID: "vm-b", duration: 7)
        box.add(voiceNote: first, session: "s", at: t0)
        box.add(voiceNote: second, session: "s", at: t0 + 5)
        for id in ["vm-a", "vm-b"] { box.heard(id, "Yes, go ahead"); box.mark(id, .sent) }
        func row(_ id: String, _ clip: String?, _ at: TimeInterval) -> HistoryThreadMessage {
            var m = HistoryThreadMessage(id: id, role: .user, text: "Yes, go ahead", at: t0 + at, voice: true)
            m.voiceClip = clip; return m
        }
        // Only the second recording's row is on the page so far: the first stays on its way, and
        // the second never claims a row of the first.
        let onlySecond = [row("m2", "vm-b", 6)]
        var shown = box.merged(into: onlySecond, session: "s")
        XCTAssertEqual(shown.map(\.id), ["m2", "local-vm-a"])
        XCTAssertEqual(shown.last?.voiceClip, "vm-a")
        XCTAssertEqual(shown.last?.voiceDuration, 2)
        box.prune(onlySecond, session: "s")
        XCTAssertEqual(box.items.map(\.id), ["vm-a"], "same words, different recording: still on its way")
        // After a reload both rows are there, each with its own clip.
        let both = [row("m1", "vm-a", 1), row("m2", "vm-b", 6)]
        shown = box.merged(into: both, session: "s")
        XCTAssertEqual(shown.map(\.id), ["m1", "m2"])
        XCTAssertEqual(shown.map(\.voiceClip), ["vm-a", "vm-b"])
        box.prune(both, session: "s")
        XCTAssertTrue(box.items.isEmpty)
        // Rows from the server carry the clip as voice_clip, or only as the client id.
        let entry = try XCTUnwrap(HistoryEntry(json: ["id": "m3", "kind": "user", "text": "Yes, go ahead", "voice": true,
                                                      "client_id": "vm-c"]))
        XCTAssertEqual(entry.voiceClip, "vm-c")
        XCTAssertNil(HistoryEntry(json: ["id": "m4", "kind": "user", "text": "typed", "client_id": "c9"])?.voiceClip)
    }

    func testOutboxShowsPendingUntilEchoed() throws {
        var box = HistoryOutbox()
        let t0 = Date(timeIntervalSince1970: 1_000_000)
        box.add(try XCTUnwrap(HistoryMessageRequest(sessionID: "s", text: "yes", source: .voiceMessage, clientID: "c1")), at: t0)
        let earlierYes = HistoryThreadMessage(id: "m1", role: .user, text: "yes", at: t0 - 3600)
        var shown = box.merged(into: [earlierYes], session: "s")
        XCTAssertEqual(shown.count, 2, "an earlier identical message never hides a new one")
        XCTAssertEqual(shown.last?.delivery, .sending)
        XCTAssertEqual(shown.last?.voice, true)
        XCTAssertEqual(shown.last?.clientID, "c1")
        XCTAssertTrue(box.merged(into: [], session: "other").isEmpty)
        XCTAssertTrue(box.pending(in: "s"))
        box.mark("c1", .failed("Couldn't reach your Hermes."))
        XCTAssertEqual(box.merged(into: [], session: "s").last?.delivery, .failed("Couldn't reach your Hermes."))
        box.mark("c1", .sent)
        XCTAssertFalse(box.pending(in: "s"), "a sent message alone doesn't keep the page polling fast")
        let unnamed = HistoryThreadMessage(id: "m2", role: .user, text: "yes", at: t0 + 2)
        XCTAssertEqual(box.merged(into: [earlierYes, unnamed], session: "s").count, 3, "words never match (SPK-80)")
        var echo = unnamed; echo.clientID = "c1"
        shown = box.merged(into: [earlierYes, echo], session: "s")
        XCTAssertEqual(shown.map(\.id), ["m1", "m2"], "the server's copy replaces ours")
        box.prune([earlierYes, echo], session: "s")
        XCTAssertTrue(box.items.isEmpty)
    }

    // MARK: Thread

    func testThreadFoldsToolsAndPullsOutWaitingAndHelpers() throws {
        let json = try JSONSerialization.jsonObject(with: Data("""
        {"session": {"id": "s1", "title": "Shelf", "source": "discord", "source_label": "Discord · #build", "state": "needs_you",
          "send_via": "hermes",
          "entries": [
            {"kind": "user", "id": "m1", "at": 1793891100.0, "text": "Size the brackets"},
            {"kind": "tools", "id": "t2", "items": [{"n": 1, "text": "Ran a command", "tool": "terminal", "status": "done"},
                                                     {"n": 2, "text": "Read plan.md", "tool": "read_file", "status": "failed"}]},
            {"kind": "note", "id": "m3", "text": "A helper reported back"},
            {"kind": "assistant", "id": "m4", "text": "Three brackets."},
            {"kind": "user", "id": "m5", "text": "Order them please"},
            {"kind": "tools", "id": "t6", "items": [{"n": 3, "text": "Opened the shop", "tool": "browser", "status": "running"}]}],
          "approvals": [{"run_id": "run_7", "description": "Run rm -rf build/", "choices": ["once", "session", "always", "deny"]}],
          "jobs": [
            {"run_id": "run_7", "status": "waiting_for_approval", "detail": "Run rm -rf build/",
             "subagents": [{"id": "h1", "title": "Check the stock", "status": "running"}]},
            {"run_id": "run_8", "status": "completed",
             "result": {"views": [{"kind": "question", "question": "Oak or walnut?", "options": [{"text": "Oak"}, {"text": "Walnut"}]}]},
             "subagents": [{"id": "h1", "title": "Check the stock", "status": "running"}]}]}}
        """.utf8))
        let c = try XCTUnwrap(HistoryConversation(json: json))
        let t = HistoryThread(c)
        XCTAssertEqual(t.messages.map(\.id), ["m1", "m3", "m4", "m5", "tools-end"])
        XCTAssertEqual(t.messages[2].tools.map(\.text), ["Ran a command", "Read plan.md"], "tools fold into the answer, past the note")
        XCTAssertEqual(t.messages[4].tools.map(\.status), [.running], "trailing tools: work in progress")
        XCTAssertEqual(t.toolCallCount, 3)
        XCTAssertEqual(t.helpers.map(\.id), ["h1"], "a helper in two jobs shows once")
        let job = try XCTUnwrap(t.waiting.first { $0.runID == "run_7" })
        XCTAssertEqual(job.kind, .approval, "a task's approval: answered on POST …/approval by run id")
        XCTAssertEqual(job.text, "Run rm -rf build/")
        XCTAssertEqual(job.choices, ["once", "session", "deny"])
        let question = try XCTUnwrap(t.waiting.first { $0.kind == .question })
        XCTAssertEqual(question.choices, ["Oak", "Walnut"])
        XCTAssertNil(question.runID, "a task's question is answered by a message to the session")
    }

    // MARK: Voice messages on the Mac

    func testLiveCallHidesMicAndDropsRecording() {
        var v = MacVoiceMessage()
        XCTAssertEqual(v.handle(.micTapped(now: Date())), .askPermissions)
        XCTAssertEqual(v.handle(.permissions(granted: true, onDevice: true)), .startRecording)
        v.handle(.partial("half a thought"))
        XCTAssertEqual(v.setCallLive(true), .cancelRecording, "the call takes the mic")
        XCTAssertFalse(v.micVisible)
        XCTAssertEqual(v.flow.phase, .idle)
        XCTAssertEqual(v.handle(.micTapped(now: Date())), .none, "no voice messages during a call")
        XCTAssertEqual(v.setCallLive(false), .none)
        XCTAssertTrue(v.micVisible)
        // A transcript under review is just text in the field: it survives a call starting.
        XCTAssertEqual(v.handle(.micTapped(now: Date())), .startRecording, "permission remembered")
        v.handle(.micTapped(now: Date()))
        v.handle(.final("keep me"))
        XCTAssertTrue(v.reviewing)
        XCTAssertEqual(v.setCallLive(true), .none)
        XCTAssertTrue(v.reviewing)
        XCTAssertEqual(v.flow.fieldText, "keep me")
    }

    func testMacWordingForProblems() {
        var v = MacVoiceMessage()
        v.handle(.micTapped(now: Date()))
        v.handle(.permissions(granted: true, onDevice: false))
        XCTAssertEqual(v.problem?.contains("This Mac"), true, "the shared text says iPhone")
        var d = MacVoiceMessage()
        d.handle(.micTapped(now: Date()))
        d.handle(.permissions(granted: false, onDevice: true))
        XCTAssertEqual(d.problem?.contains("System Settings"), true)
    }

    // MARK: Pins, archive, live merges (deep pass)

    private func s(_ id: String, pinned: Bool = false, archived: Bool = false, minutes: Double = 1) -> HistorySession {
        HistorySession(id: id, title: id, source: "cli", sourceLabel: "Terminal",
                       lastActivity: Date().addingTimeInterval(-minutes * 60), pinned: pinned, archived: archived)
    }

    func testPinnedRowsLeadAndArchivedMatchesFollowASearch() {
        var list = HistoryList()
        let q = list.reset()
        list.apply(HistoryPage(sessions: [s("p", pinned: true), s("a"), s("old", archived: true)]), for: q)
        XCTAssertEqual(list.pinned.map(\.id), ["p"])
        XCTAssertEqual(list.grouped(now: Date()).flatMap(\.1).map(\.id), ["a"], "no pinned or archived rows in the day groups")
        XCTAssertEqual(list.archivedMatches.map(\.id), ["old"])
        let arch = list.setArchived(true)!
        XCTAssertTrue(arch.path.contains("archived=1"))
        list.apply(HistoryPage(sessions: [s("old", archived: true)]), for: arch)
        XCTAssertTrue(list.archivedMatches.isEmpty && list.pinned.isEmpty)
        XCTAssertEqual(list.grouped(now: Date()).flatMap(\.1).map(\.id), ["old"])
    }

    func testALiveMergeUpdatesInPlaceAndDropsArchived() {
        var list = HistoryList()
        let q = list.reset()
        list.apply(HistoryPage(sessions: [s("a"), s("b"), s("c")], nextCursor: "x"), for: q)
        var fresh = HistoryQuery(limit: HistoryList.pageSize, generation: list.generation)
        fresh.q = ""
        list.merge(HistoryPage(sessions: [s("c"), s("a")]), for: fresh)
        XCTAssertEqual(list.sessions.map(\.id), ["c", "a"], "c moved to the top; b (archived elsewhere) left")
    }

    func testDeepLinks() {
        let url = HistorySession.link("20261105_155402_4c4275de")!
        XCTAssertEqual(url.absoluteString, "speakeasy://session/20261105_155402_4c4275de")
        XCTAssertEqual(HistorySession.sessionID(fromLink: url), "20261105_155402_4c4275de")
    }

    func testOnlyTheFlaggedQuestionWaits() throws {
        let json = try JSONSerialization.jsonObject(with: Data("""
        {"session": {"id": "s1", "title": "T", "source": "api_server", "source_label": "Speakeasy", "state": "idle",
          "send_via": "task", "entries": [],
          "jobs": [
            {"run_id": "r2", "status": "completed", "question_open": false, "set_aside": true,
             "result": {"views": [{"kind": "question", "question": "Ship?", "options": [{"text": "Yes"}, {"text": "No"}]}]}},
            {"run_id": "r1", "status": "completed", "question_open": false,
             "result": {"views": [{"kind": "question", "question": "Ship?", "options": [{"text": "Yes"}, {"text": "No"}]}]}}]}}
        """.utf8))
        let c = try XCTUnwrap(HistoryConversation(json: json))
        XCTAssertTrue(c.waitingQuestions.isEmpty, "answered on another device: no card, not twice")
    }

    // MARK: Decision cards (dismiss, reply, moved on)

    private func questionJob(_ run: String, _ q: String, open: Bool?, closed: String? = nil) -> String {
        var extra = open.map { ", \"question_open\": \($0)" } ?? ""
        if let closed { extra += ", \"question_closed\": \"\(closed)\", \"set_aside\": true" }
        return """
        {"run_id": "\(run)", "status": "completed", "title": "Garden task"\(extra),
         "result": {"views": [{"kind": "question", "question": "\(q)", "options": [{"text": "Green"}, {"text": "Grey"}]}]}}
        """
    }

    private func conversation(_ jobs: [String]) throws -> HistoryConversation {
        let json = try JSONSerialization.jsonObject(with: Data("""
        {"session": {"id": "s1", "title": "T", "source": "discord", "source_label": "Discord", "state": "needs_you",
          "send_via": "linked", "entries": [], "jobs": [\(jobs.joined(separator: ","))]}}
        """.utf8))
        return try XCTUnwrap(HistoryConversation(json: json))
    }

    func testTwoOpenQuestionsAreTwoIndependentCards() throws {
        let c = try conversation([questionJob("r2", "Paint the shed green or grey?", open: true),
                                  questionJob("r1", "Order the cedar boards now?", open: true)])
        XCTAssertEqual(c.waitingQuestions.map(\.runID), ["r2", "r1"])
        let thread = HistoryThread(c)
        XCTAssertEqual(thread.waiting.map(\.questionRunID), ["r2", "r1"], "each card answers and dismisses its own task")
    }

    func testDismissingOneLeavesTheOther() throws {
        let c = try conversation([questionJob("r2", "Paint the shed green or grey?", open: false, closed: "dismissed"),
                                  questionJob("r1", "Order the cedar boards now?", open: true)])
        XCTAssertEqual(c.waitingQuestions.map(\.runID), ["r1"])
    }

    func testMovedOnQuestionIsAQuietLineNotACard() throws {
        let c = try conversation([questionJob("r1", "Push the plugin and install it?", open: false, closed: "moved_on")])
        XCTAssertTrue(c.waitingQuestions.isEmpty)
        let job = try XCTUnwrap(c.jobs.first)
        XCTAssertEqual(job.questionClosed, "moved_on")
        XCTAssertEqual(TaskLineCopy.questionNote(job), "Question · moved on · Push the plugin and install it?")
        var dismissed = job; dismissed.questionClosed = "dismissed"
        XCTAssertEqual(TaskLineCopy.questionNote(dismissed), "Question · dismissed · Push the plugin and install it?")
        XCTAssertTrue(TaskLineCopy.line(job).hasPrefix("Question · moved on"))
    }

    func testTheSameQuestionTwiceIsOneCard() throws {
        let c = try conversation([questionJob("r2", "Paint the shed green or grey?", open: true),
                                  questionJob("r1", "Paint the shed green or grey?", open: true)])
        XCTAssertEqual(c.waitingQuestions.map(\.runID), ["r2"])
    }

    func testReplyCarriesTheQuestionItAnswers() throws {
        let r = try XCTUnwrap(HistoryMessageRequest(sessionID: "s1", text: "Grey, but only the door", clientID: "c1", answers: "r1"))
        XCTAssertEqual(r.body["answers"] as? String, "r1")
        let plain = try XCTUnwrap(HistoryMessageRequest(sessionID: "s1", text: "Thanks", clientID: "c2"))
        XCTAssertNil(plain.body["answers"])
        guard case .message(let m)? = HistoryTarget.session("s1").outgoing("Grey", source: .typed, answers: "r1") else {
            return XCTFail("a session message")
        }
        XCTAssertEqual(m.answers, "r1")
        let reply = try XCTUnwrap(HistoryMessageReply(json: ["ok": true, "via": "answer", "run_id": "r1"]))
        XCTAssertEqual(reply.via, .answer)
    }
}
