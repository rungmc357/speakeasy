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
        let echo = HistoryThreadMessage(id: "m2", role: .user, text: "yes", at: t0 + 2)
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
             "subagents": [{"id": "h1", "title": "Check the stock", "status": "running"}]}],
          "sends": [
            {"send_id": "hs_1", "status": "completed", "text": "Order them please", "reply": "Ordered."},
            {"send_id": "hs_2", "status": "needs_you", "text": "And the screws",
             "prompt": {"kind": "approval", "prompt_id": "p1", "text": "Buy screws for $4?", "choices": ["once", "session", "always", "deny"]}},
            {"send_id": "hs_3", "status": "failed", "text": "Ship it", "error": "Hermes isn't ready to take that message."}]}}
        """.utf8))
        let c = try XCTUnwrap(HistoryConversation(json: json))
        let t = HistoryThread(c)
        XCTAssertEqual(t.messages.map(\.id), ["m1", "m3", "m4", "m5", "tools-end", "send-hs_2", "send-hs_3"])
        XCTAssertEqual(t.messages[2].tools.map(\.text), ["Ran a command", "Read plan.md"], "tools fold into the answer, past the note")
        XCTAssertEqual(t.messages[4].tools.map(\.status), [.running], "trailing tools: work in progress")
        XCTAssertEqual(t.toolCallCount, 3)
        XCTAssertEqual(t.messages[5].delivery, .sent)
        XCTAssertEqual(t.messages[6].delivery, .failed("Hermes isn't ready to take that message."))
        XCTAssertEqual(t.helpers.map(\.id), ["h1"], "a helper in two jobs shows once")
        let send = try XCTUnwrap(t.waiting.first { $0.sendID == "hs_2" })
        XCTAssertEqual(send.kind, .approval)
        XCTAssertEqual(send.choices, ["once", "session", "deny"], "never always")
        let job = try XCTUnwrap(t.waiting.first { $0.runID == "run_7" })
        XCTAssertNil(job.sendID)
        XCTAssertEqual(job.kind, .approval, "a task's approval: answered on POST …/approval by run id")
        XCTAssertEqual(job.text, "Run rm -rf build/")
        XCTAssertEqual(job.choices, ["once", "session", "deny"])
        let question = try XCTUnwrap(t.waiting.first { $0.kind == .question })
        XCTAssertEqual(question.choices, ["Oak", "Walnut"])
        XCTAssertNil(question.runID, "a task's question is answered by a message to the session")
        XCTAssertNil(question.sendID)
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
}
