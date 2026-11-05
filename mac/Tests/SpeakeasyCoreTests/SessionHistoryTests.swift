import XCTest
@testable import SpeakeasyCore

final class SessionHistoryTests: XCTestCase {
    private func session(_ id: String, _ title: String = "T", at: Date? = nil) -> HistorySession {
        HistorySession(id: id, title: title, updatedAt: at)
    }

    // MARK: Capability + decoding (the history lane's documented shapes)

    func testCapabilityFlagDecodes() throws {
        let on = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"task_messages":true,"history":true}"#.utf8))
        XCTAssertTrue(on.history)
        let old = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"task_messages":true}"#.utf8))
        XCTAssertFalse(old.history, "an older plugin shows the update note instead of the list")
    }

    func testListPageDecodesDocumentedShape() throws {
        let json = try JSONSerialization.jsonObject(with: Data("""
        {"sessions": [
          {"id": "20261104_093012_ab12cd", "title": "Shelf brackets", "source": "discord",
           "source_label": "Discord · #build", "last_activity": 1793891284.09,
           "preview": "Three brackets at 30 cm spacing\\nwill hold it.", "state": "idle", "match": "size the walnut shelf brackets"},
          {"id": "speakeasy_task_2a85", "title": "Bake sale flyer", "source": "speakeasy", "source_label": "Speakeasy",
           "last_activity": 1793890001.2, "preview": "Flyer drafted.", "state": "running", "run_ids": ["run_0002", "run_0001"]},
          {"id": "s3", "state": "needs_you", "preview": "Which plan?"},
          {"title": "no id: dropped"}],
         "next_cursor": "1793890001.2|speakeasy_task_2a85",
         "filters": [{"id": "speakeasy", "label": "Speakeasy"}, {"id": "discord", "label": "Discord"}, {"id": "scheduled", "label": "Scheduled"}],
         "search": "fts"}
        """.utf8))
        let page = try XCTUnwrap(HistoryPage(json: json))
        XCTAssertEqual(page.sessions.map(\.id), ["20261104_093012_ab12cd", "speakeasy_task_2a85", "s3"])
        let a = page.sessions[0]
        XCTAssertEqual(a.sourceName, "Discord · #build")
        XCTAssertEqual(a.preview, "Three brackets at 30 cm spacing will hold it.", "one line")
        XCTAssertEqual(a.snippet, "size the walnut shelf brackets")
        XCTAssertEqual(a.updatedAt, Date(timeIntervalSince1970: 1793891284.09))
        XCTAssertEqual(a.status, .idle)
        XCTAssertEqual(page.sessions[1].status, .running)
        XCTAssertEqual(page.sessions[1].runID, "run_0002", "newest task first")
        XCTAssertEqual(page.sessions[2].status, .waiting)
        XCTAssertEqual(page.sessions[2].title, "Which plan?", "untitled falls back to the preview")
        XCTAssertEqual(page.nextCursor, "1793890001.2|speakeasy_task_2a85")
        XCTAssertEqual(page.sources.map(\.id), ["speakeasy", "discord", "scheduled"])
    }

    func testDetailDecodesEntriesFoldsToolsAndReadsJobsAndSends() throws {
        let json = try JSONSerialization.jsonObject(with: Data("""
        {"session": {"id": "s1", "title": "Shelf brackets", "source": "discord", "source_label": "Discord · #build",
          "state": "needs_you", "send_via": "hermes",
          "entries": [
            {"kind": "user", "id": "m101", "at": 1793891100.0, "text": "Can you size the walnut shelf brackets?"},
            {"kind": "tools", "id": "t102", "items": [
               {"n": 1, "text": "Ran a command", "tool": "terminal", "status": "done", "args": "ls", "result": "ok",
                "started_at": 1793891101.0, "ended_at": 1793891103.2},
               {"n": 2, "text": "Read plan.md", "tool": "read_file", "status": "failed"}]},
            {"kind": "note", "id": "m104", "at": 1793891150.0, "text": "A helper reported back"},
            {"kind": "assistant", "id": "m105", "at": 1793891284.1, "text": "Three brackets at 30 cm spacing will hold it."},
            {"kind": "user", "id": "m106", "text": "Order them please"}],
          "has_more": true, "before": 101,
          "jobs": [
            {"run_id": "run_7", "status": "waiting_for_approval", "detail": "Run rm -rf build/", "title": "Clean",
             "subagents": [{"id": "h1", "title": "Check the stock", "status": "running"}]},
            {"run_id": "run_8", "status": "completed", "title": "Pick",
             "result": {"views": [{"kind": "question", "question": "Oak or walnut?", "options": [{"text": "Oak"}, {"text": "Walnut"}]}]}}],
          "sends": [
            {"send_id": "hs_1", "status": "completed", "text": "Order them please", "reply": "Ordered.", "started": 1793891300.0},
            {"send_id": "hs_2", "status": "needs_you", "text": "And the screws", "started": 1793891400.0,
             "prompt": {"kind": "approval", "prompt_id": "p1", "text": "Buy screws for $4?", "choices": ["once", "session", "always", "deny"]}},
            {"send_id": "hs_3", "status": "failed", "text": "Ship it", "error": "Hermes isn't ready to take that message. Try again in a moment."}]}}
        """.utf8))
        let d = try XCTUnwrap(HistoryDetail(json: json, id: "s1"))
        XCTAssertEqual(d.session.title, "Shelf brackets")
        XCTAssertEqual(d.session.status, .waiting)
        XCTAssertTrue(d.hasMore)
        XCTAssertEqual(d.sendVia, "hermes")
        // The tools row folds into the answer it led to; the note is a system row.
        XCTAssertEqual(d.messages.map(\.role), [.user, .system, .assistant, .user, .user, .user])
        let answer = d.messages[2]
        XCTAssertEqual(answer.toolCalls.map(\.text), ["Ran a command", "Read plan.md"])
        XCTAssertEqual(answer.toolCalls.map(\.status), [.done, .failed])
        XCTAssertEqual(d.toolCallCount, 2)
        // Sends: completed and echoed → not repeated; needs_you → a bubble + an approval with its choices
        // (never "always"); failed → a failed bubble.
        XCTAssertEqual(d.messages[4].text, "And the screws")
        XCTAssertEqual(d.messages[4].delivery, .sent)
        XCTAssertEqual(d.messages[5].delivery, .failed("Hermes isn't ready to take that message. Try again in a moment."))
        XCTAssertEqual(d.helpers.map(\.id), ["h1"])
        let sendApproval = try XCTUnwrap(d.approvals.first { $0.sendID == "hs_2" })
        XCTAssertEqual(sendApproval.choices, ["once", "session", "deny"])
        XCTAssertEqual(d.approvals.first { $0.sendID == nil }?.description, "Run rm -rf build/")
        XCTAssertEqual(d.questions.map(\.question), ["Oak or walnut?"])
        XCTAssertEqual(d.questions.first?.options, ["Oak", "Walnut"])
    }

    func testDetailFlatMessagesFallback() throws {
        let d = try XCTUnwrap(HistoryDetail(json: ["messages": [["role": "user", "content": "hi"], ["role": "assistant", "text": "hello"]]], id: "x"))
        XCTAssertEqual(d.session.id, "x")
        XCTAssertEqual(d.messages.map(\.text), ["hi", "hello"])
    }

    // MARK: List paging

    func testPagingAppendsDedupesAndEnds() {
        var list = HistoryList()
        let q1 = list.reset()
        XCTAssertNil(q1.cursor)
        XCTAssertEqual(q1.limit, HistoryList.pageSize)
        XCTAssertTrue(list.loading)
        XCTAssertNil(list.loadMore(), "one page at a time")
        XCTAssertTrue(list.apply(HistoryPage(sessions: [session("a"), session("b")], nextCursor: "c1"), for: q1))
        XCTAssertFalse(list.loading)
        let q2 = try! XCTUnwrap(list.loadMore())
        XCTAssertEqual(q2.cursor, "c1")
        XCTAssertTrue(q2.path.contains("cursor=c1"))
        // b again (it moved while paging): updated in place, not repeated.
        list.apply(HistoryPage(sessions: [session("b", "B2"), session("c")], nextCursor: nil), for: q2)
        XCTAssertEqual(list.sessions.map(\.id), ["a", "b", "c"])
        XCTAssertEqual(list.sessions[1].title, "B2")
        XCTAssertTrue(list.reachedEnd)
        XCTAssertNil(list.loadMore(), "no cursor: the end")
    }

    func testPagingErrorAndRetry() {
        var list = HistoryList()
        let q = list.reset()
        list.fail("Couldn't reach your Hermes.", for: q)
        XCTAssertEqual(list.error, "Couldn't reach your Hermes.")
        XCTAssertNil(list.loadMore(), "an error stops auto-paging")
        let retry = try! XCTUnwrap(list.retry())
        XCTAssertNil(list.error)
        XCTAssertEqual(retry.cursor, nil)
    }

    func testUpsertMovesToTop() {
        var list = HistoryList()
        let q = list.reset()
        list.apply(HistoryPage(sessions: [session("a"), session("b")], nextCursor: nil), for: q)
        list.upsert(session("b", "new"))
        XCTAssertEqual(list.sessions.map(\.id), ["b", "a"])
        XCTAssertEqual(list.sessions[0].title, "new")
    }

    func testGroupsTodayYesterdayEarlier() {
        var cal = Calendar(identifier: .gregorian); cal.timeZone = TimeZone(identifier: "America/New_York")!
        let now = cal.date(from: DateComponents(year: 2026, month: 11, day: 5, hour: 9))!
        var list = HistoryList()
        let q = list.reset()
        list.apply(HistoryPage(sessions: [
            session("t", at: cal.date(from: DateComponents(year: 2026, month: 11, day: 5, hour: 0, minute: 5))),
            session("y", at: cal.date(from: DateComponents(year: 2026, month: 11, day: 4, hour: 23))),
            session("e", at: cal.date(from: DateComponents(year: 2026, month: 10, day: 30))),
            session("n", at: nil)], nextCursor: nil), for: q)
        let groups = list.grouped(now: now, calendar: cal)
        XCTAssertEqual(groups.map(\.0), [.today, .yesterday, .earlier])
        XCTAssertEqual(groups.map { $0.1.map(\.id) }, [["t"], ["y"], ["e", "n"]])
    }

    // MARK: Search + filter state

    func testSearchStartsOverAndDropsStaleReplies() {
        var list = HistoryList()
        let first = list.reset()
        list.apply(HistoryPage(sessions: [session("a")], nextCursor: "c"), for: first)
        let search = try! XCTUnwrap(list.setQuery("walnut"))
        XCTAssertEqual(search.q, "walnut")
        XCTAssertNil(search.cursor, "a new search starts at the top")
        XCTAssertTrue(list.sessions.isEmpty)
        XCTAssertTrue(list.isFiltered)
        XCTAssertNil(list.setQuery("walnut "), "trailing space: same search, no request")
        let narrower = try! XCTUnwrap(list.setQuery("walnut shelf"))
        // The reply to the first search arrives late: dropped.
        XCTAssertFalse(list.apply(HistoryPage(sessions: [session("old")], nextCursor: nil), for: search))
        XCTAssertFalse(list.fail("x", for: search))
        XCTAssertTrue(list.apply(HistoryPage(sessions: [session("hit")], nextCursor: nil), for: narrower))
        XCTAssertEqual(list.sessions.map(\.id), ["hit"])
        XCTAssertNil(list.error)
        // Clearing the search starts over unfiltered.
        let cleared = try! XCTUnwrap(list.setQuery("   "))
        XCTAssertNil(cleared.q)
        XCTAssertFalse(list.isFiltered)
    }

    func testSourceFilterAndQueryPath() {
        var list = HistoryList()
        _ = list.reset()
        let q = try! XCTUnwrap(list.setSource("discord"))
        XCTAssertEqual(q.source, "discord")
        XCTAssertNil(list.setSource("discord"), "same filter: no request")
        let both = try! XCTUnwrap(list.setQuery("a+b & c"))
        XCTAssertEqual(both.path, "/voice/history?limit=40&q=a%2Bb%20%26%20c&source=discord")
        XCTAssertEqual(list.sourceChoices.map(\.id), HistorySource.defaults.map(\.id), "until the server lists its own")
        list.apply(HistoryPage(sessions: [], nextCursor: nil, sources: [HistorySource(id: "gui", label: "Desktop")]), for: both)
        XCTAssertEqual(list.sourceChoices.map(\.label), ["Desktop"])
    }

    // MARK: Send by id

    func testSessionPageSendsToThatSessionByID() {
        let out = HistoryTarget.session("2026/11 a").outgoing("  Order them please  ", source: .typed, clientID: "c1")
        guard case .message(let r)? = out else { return XCTFail("a session page sends a message to that session") }
        XCTAssertEqual(r.sessionID, "2026/11 a")
        XCTAssertEqual(r.path, "/voice/history/2026%2F11%20a/message", "by id, escaped: never the router")
        XCTAssertEqual(r.body["text"] as? String, "Order them please")
        XCTAssertEqual(r.body["source"] as? String, "typed")
        XCTAssertEqual(r.body["client_id"] as? String, "c1")
        let voice = HistoryTarget.session("s").outgoing("hi", source: .voice, clientID: "c2")
        guard case .message(let v)? = voice else { return XCTFail() }
        XCTAssertEqual(v.body["source"] as? String, "voice_message", "the lane's name for transcribed voice messages")
        XCTAssertNil(HistoryTarget.session("s").outgoing("   ", source: .typed), "nothing to send")
    }

    func testNewChatStartsWork() {
        guard case .newChat(let text, _, _)? = HistoryTarget.newChat.outgoing(" Plan the offsite ", source: .typed) else {
            return XCTFail("New chat goes to POST /voice/work")
        }
        XCTAssertEqual(text, "Plan the offsite")
        XCTAssertNil(HistoryTarget.newChat.sessionID)
    }

    func testSendAnswerPathAndReply() {
        let a = HistorySendAnswer(sessionID: "s 1", sendID: "hs_2", choice: "once")
        XCTAssertEqual(a.path, "/voice/history/s%201/sends/hs_2/answer")
        XCTAssertEqual(a.body["choice"] as? String, "once")
        let reply = HistoryMessageReply(json: ["ok": true, "session_id": "s", "via": "hermes", "run_id": NSNull(),
                                               "delivered_as": "message", "send_id": "hs_9"])
        XCTAssertEqual(reply?.via, "hermes")
        XCTAssertEqual(reply?.sendID, "hs_9")
        XCTAssertEqual(reply?.deliveredAs, .unknown)
        XCTAssertNil(HistoryMessageReply(json: ["error": "nope"]))
    }

    func testOutboxShowsPendingUntilEchoed() {
        var box = HistoryOutbox()
        let t0 = Date(timeIntervalSince1970: 1_000_000)
        let r = HistoryMessageRequest(sessionID: "s", text: "yes", clientID: "c1")
        box.add(r, at: t0)
        let earlierYes = HistoryMessage(id: "m1", role: .user, text: "yes", at: t0 - 3600)
        var shown = box.merged(into: [earlierYes], session: "s")
        XCTAssertEqual(shown.count, 2, "an earlier identical message never hides a new one")
        XCTAssertEqual(shown.last?.delivery, .sending)
        XCTAssertTrue(box.merged(into: [], session: "other").isEmpty, "only that session's")
        box.mark("c1", .failed("Couldn't reach your Hermes."))
        XCTAssertEqual(box.merged(into: [], session: "s").last?.delivery, .failed("Couldn't reach your Hermes."))
        box.mark("c1", .sent)
        let echo = HistoryMessage(id: "m2", role: .user, text: "yes", at: t0 + 2)
        shown = box.merged(into: [earlierYes, echo], session: "s")
        XCTAssertEqual(shown.map(\.id), ["m1", "m2"], "the server's copy replaces ours")
        box.prune([earlierYes, echo], session: "s")
        XCTAssertTrue(box.items.isEmpty)
    }

    // MARK: Voice-message state machine

    func testVoiceMessageHappyPath() {
        var f = VoiceMessageFlow()
        let t = Date(timeIntervalSince1970: 100)
        XCTAssertTrue(f.micVisible)
        XCTAssertTrue(f.tapMic())
        XCTAssertEqual(f.phase, .asking)
        XCTAssertFalse(f.tapMic(), "already starting")
        f.permission(granted: true, now: t)
        XCTAssertTrue(f.isRecording)
        f.heard("order the")
        f.heard("order the walnut ones")
        XCTAssertEqual(f.elapsed(now: t + 5), 5)
        f.stop()
        XCTAssertEqual(f.phase, .finishing(heard: "order the walnut ones"))
        f.heard("Order the walnut ones.")
        f.finished(nil)
        XCTAssertEqual(f.reviewText, "Order the walnut ones.")
        f.edit("Order the oak ones.")
        XCTAssertEqual(f.send(), "Order the oak ones.")
        XCTAssertEqual(f.phase, .sending("Order the oak ones."))
        f.sendFailed()
        XCTAssertEqual(f.reviewText, "Order the oak ones.", "a failed send keeps the words")
        _ = f.send(); f.sent()
        XCTAssertEqual(f.phase, .idle)
    }

    func testVoiceMessageProblemsAndCancel() {
        var f = VoiceMessageFlow()
        _ = f.tapMic(); f.permission(granted: false, now: Date())
        guard case .problem = f.phase else { return XCTFail("no permission: a problem, not a recording") }
        XCTAssertTrue(f.tapMic(), "try again from a problem")
        f.permission(granted: true, now: Date())
        f.stop(); f.finished("   ")
        guard case .problem = f.phase else { return XCTFail("nothing heard") }
        _ = f.tapMic(); f.permission(granted: true, now: Date()); f.heard("hi")
        f.cancel()
        XCTAssertEqual(f.phase, .idle)
        XCTAssertNil(f.send(), "nothing to send when idle")
        _ = f.tapMic(); f.permission(granted: true, now: Date()); f.stop(); f.finished("x")
        f.edit("  ")
        XCTAssertNil(f.send(), "an emptied transcript can't be sent")
    }

    func testVoiceMessageTimeLimit() {
        var f = VoiceMessageFlow()
        let t = Date(timeIntervalSince1970: 0)
        _ = f.tapMic(); f.permission(granted: true, now: t)
        XCTAssertFalse(f.overLimit(now: t + 60))
        XCTAssertTrue(f.overLimit(now: t + VoiceMessageFlow.maxSeconds))
    }

    func testLiveCallHidesMicAndDropsRecording() {
        var f = VoiceMessageFlow()
        _ = f.tapMic(); f.permission(granted: true, now: Date()); f.heard("half a thought")
        f.setCallLive(true)
        XCTAssertFalse(f.micVisible)
        XCTAssertEqual(f.phase, .idle, "the call takes the mic: the recording is dropped")
        XCTAssertFalse(f.tapMic(), "no voice messages during a call")
        f.setCallLive(false)
        XCTAssertTrue(f.micVisible)
        // A transcript under review is just text: it survives a call starting.
        _ = f.tapMic(); f.permission(granted: true, now: Date()); f.stop(); f.finished("keep me")
        f.setCallLive(true)
        XCTAssertEqual(f.reviewText, "keep me")
    }
}
