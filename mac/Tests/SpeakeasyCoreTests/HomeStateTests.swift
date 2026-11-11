import XCTest
@testable import SpeakeasyCore

/// Build 73's Home state: the app decodes the plugin's real b73 /voice/now reply
/// (Contract/now_b73.json, written by plugin/tests/test_b73_home_state.py from invented data), and an
/// older plugin's reply (Contract/now.json, no b73 keys) still gives a current exchange, a stack and
/// recommendations the app worked out itself.
final class HomeStateTests: XCTestCase {
    private func contract(_ name: String) throws -> [String: Any] {
        let url = try XCTUnwrap(Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Contract"))
        return try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    }

    // MARK: new plugin

    func testDecodesTheCurrentExchange() throws {
        let home = NowHome(json: try contract("now_b73"))
        let stream = try XCTUnwrap(home.stream)
        let cur = try XCTUnwrap(stream.current, "the plugin sent stream.current")
        XCTAssertFalse(cur.clean)
        XCTAssertFalse(cur.derived)
        XCTAssertEqual(cur.entryIDs.count, 3)
        XCTAssertEqual(cur.entryIDs.first, cur.messageID)
        XCTAssertEqual(cur.cardIDs, ["c:run_inbox"])
        XCTAssertEqual(cur.runIDs, ["run_inbox"])
        XCTAssertEqual(cur.replyIDs.count, 1)
        let activity = try XCTUnwrap(cur.lastActivityAt), resets = try XCTUnwrap(cur.resetsAt)
        XCTAssertEqual(resets.timeIntervalSince(activity), HomeCurrent.resetAfter, accuracy: 0.01)
        // Each entry of it carries `current`, and nothing else does.
        XCTAssertEqual(stream.entries.filter(\.current).map(\.id), cur.entryIDs.sorted { a, b in
            stream.entries.firstIndex { $0.id == a }! < stream.entries.firstIndex { $0.id == b }! })
        // The older exchange is still in the transcript, just not current.
        XCTAssertGreaterThan(stream.entries.filter { $0.kind == .message && !$0.current }.count, 0)
        // As of a moment before the reset it is current; at the reset it's clean.
        XCTAssertFalse(home.currentExchange(now: resets.addingTimeInterval(-1)).clean)
        XCTAssertEqual(stream.currentEntries(now: resets.addingTimeInterval(-1)).map(\.id).count, 3)
        XCTAssertTrue(home.currentExchange(now: resets).clean)
        XCTAssertEqual(stream.currentEntries(now: resets), [])
    }

    func testDecodesTheStack() throws {
        let home = NowHome(json: try contract("now_b73"))
        let stack = try XCTUnwrap(home.stack)
        XCTAssertFalse(stack.derived)
        XCTAssertEqual(stack.count, 4)
        XCTAssertEqual(stack.items.map(\.kind), [.decision, .stopped, .result, .result])
        XCTAssertEqual(stack.items.map(\.needsYou), [true, true, false, false])
        XCTAssertEqual(stack.needsYou.count, 2)
        let decision = stack.items[0]
        XCTAssertTrue(decision.id.hasPrefix("w:q:run_desk:"))
        XCTAssertEqual(decision.source, .init(kind: .job, runID: "run_desk", sessionID: "s_desk"))
        XCTAssertEqual(decision.cardID, "c:run_desk")
        XCTAssertEqual(decision.itemID, "q:run_desk")
        XCTAssertNil(decision.resultID)
        let inbox = try XCTUnwrap(stack.items.first { $0.id == "w:r:run_inbox" })
        XCTAssertTrue(inbox.inCurrent, "the current exchange already shows it")
        let mail = try XCTUnwrap(stack.items.first { $0.id == "w:r:run_mail" })
        XCTAssertEqual(mail.resultID, "x:run_mail")
        XCTAssertFalse(mail.inCurrent)
        XCTAssertNil(mail.dismissedOnHomeAt)
        // The result it points at is a real entry in the thread.
        XCTAssertTrue(home.stream!.entries.contains { $0.id == "x:run_mail" && $0.kind == .result })
        let parked = try XCTUnwrap(stack.onWork.first)
        XCTAssertEqual(parked.id, "w:r:run_patio")
        XCTAssertNotNil(parked.dismissedOnHomeAt)
        XCTAssertEqual(home.widgetStack(), stack, "the plugin's stack is used as is")
        // Needs you: the decision and the stopped job on the stack (none waiting on Work here).
        XCTAssertEqual(home.needsYouCount, 2)
        XCTAssertEqual(home.counts.needsYou, 1, "the plugin's own count is questions only")
    }

    func testDecodesRecommendations() throws {
        let home = NowHome(json: try contract("now_b73"))
        let recs = try XCTUnwrap(home.recommendations)
        XCTAssertFalse(recs.generic)
        XCTAssertNotNil(recs.computedAt)
        XCTAssertEqual(recs.items.map(\.source.kind), [.unsentDraft], "only what waits on you; never a follow-up on a finished job")
        XCTAssertNotNil(recs.items[0].source.draftID)
        XCTAssertEqual(recs.items[0].source.runID, "run_mail")
        XCTAssertTrue(recs.items.allSatisfy { !$0.prompt.isEmpty && !$0.generic })
        XCTAssertEqual(home.recommendationsOrFallback, recs)
    }

    func testFollowsAndUnknownKinds() {
        let item = HomeStackItem(json: ["id": "w:r:run_b", "kind": "result", "title": "Reply job", "follows": "w:r:run_a",
                                        "source": ["kind": "job", "run_id": "run_b"]])
        XCTAssertEqual(item?.follows, "w:r:run_a")
        XCTAssertEqual(item?.needsYou, false, "needs_you derives from the kind when missing")
        XCTAssertEqual(item?.cardID, "c:run_b")
        let newer = HomeStackItem(json: ["id": "w:z:x", "kind": "something_new", "needs_you": true, "title": "?"])
        XCTAssertEqual(newer?.kind, .unknown, "a kind a newer plugin added still decodes")
        XCTAssertEqual(newer?.needsYou, true)
        XCTAssertNil(HomeStackItem(json: ["id": "w:z:y", "title": "no kind"]))
        let stack = HomeStack(json: ["items": [["id": "w:s:run_c", "kind": "stopped", "title": "Plumber"]]])
        XCTAssertEqual(stack?.count, 1)
        XCTAssertEqual(stack?.items.first?.needsYou, true)
        XCTAssertEqual(stack?.items.first?.source.kind, .chat)
        let rec = HomeRecommendation(json: ["id": "rec:x", "text": "Try it", "source": ["kind": "brand_new", "label": "New"]])
        XCTAssertEqual(rec?.source.kind, .other)
        XCTAssertEqual(rec?.source.rawKind, "brand_new")
        XCTAssertEqual(rec?.prompt, "Try it")
        let gen = HomeRecommendations(json: ["items": [["id": "gen:catch-up", "text": "Catch me up"]]])
        XCTAssertEqual(gen?.generic, true)
    }

    func testDismissReplies() {
        XCTAssertEqual(HomeStackDismissal(json: ["ok": true, "id": "w:r:a", "from": "home", "state": "on_work"], id: "w:r:a", from: .home).state, .onWork)
        XCTAssertEqual(HomeStackDismissal(json: ["ok": true, "id": "w:r:a", "from": "work", "state": "gone"], id: "w:r:a", from: .work).state, .gone)
        XCTAssertEqual(HomeStackDismissal(json: [:], id: "w:r:a", from: .work).state, .gone)
    }

    func testStackAndRecommendationIDsArePercentEncodedForTheDismissRoutes() {
        XCTAssertEqual(HomeStatePath.stackDismiss("w:q:run_desk:1793984100"), "/voice/home/stack/w%3Aq%3Arun_desk%3A1793984100/dismiss")
        XCTAssertEqual(HomeStatePath.stackDismiss("w:r:run_patio"), "/voice/home/stack/w%3Ar%3Arun_patio/dismiss")
        XCTAssertEqual(HomeStatePath.recommendationDismiss("rec:parked:w:r:run_patio"),
                       "/voice/home/recommendations/rec%3Aparked%3Aw%3Ar%3Arun_patio/dismiss")
        XCTAssertEqual(HomeStatePath.recommendationDismiss("gen:catch-up"), "/voice/home/recommendations/gen%3Acatch-up/dismiss")
    }

    func testCapability() throws {
        let caps = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"home_stack": true}"#.utf8))
        XCTAssertTrue(caps.homeStack)
        XCTAssertFalse(try JSONDecoder().decode(ServerCapabilities.self, from: Data("{}".utf8)).homeStack)
    }

    // MARK: older plugin

    func testAnOlderPluginStillDecodesWithFallbacks() throws {
        let home = NowHome(json: try contract("now"))
        XCTAssertNil(home.stack)
        XCTAssertNil(home.recommendations)
        XCTAssertNil(home.stream?.current)
        XCTAssertFalse(home.stream?.entries.contains(where: \.current) ?? true)
        // No messages in that thread: Home is clean.
        let cur = home.currentExchange()
        XCTAssertTrue(cur.clean)
        XCTAssertTrue(cur.derived)
        // The stack from its items: the question, the stopped job, the new answer; never the working one.
        let stack = home.widgetStack()
        XCTAssertTrue(stack.derived)
        XCTAssertEqual(stack.items.map(\.kind), [.decision, .stopped, .result])
        XCTAssertEqual(stack.items.map(\.runID), ["run_c_q", "run_c_f", "run_c_a"])
        XCTAssertEqual(stack.items.map(\.id).dropFirst(), ["w:s:run_c_f", "w:r:run_c_a"])
        XCTAssertTrue(stack.items[0].id.hasPrefix("w:q:run_c_q:"))
        XCTAssertEqual(stack.items.map(\.itemID), ["q:run_c_q", "s:s_cf", "s:s_ca"])
        XCTAssertEqual(stack.items.map(\.cardID), ["c:run_c_q", "c:run_c_f", "c:run_c_a"])
        XCTAssertEqual(stack.onWork, [])
        XCTAssertEqual(stack.count, 3)
        XCTAssertEqual(home.needsYouCount, 2, "counts.needs_you + counts.stopped: a stopped job needs you")
        // Recommendations: the plugin's fixed generic ideas.
        let recs = home.recommendationsOrFallback
        XCTAssertTrue(recs.generic && recs.derived)
        XCTAssertEqual(recs.items.map(\.id), ["gen:catch-up", "gen:plan-week", "gen:calendar"])
        XCTAssertEqual(recs.without(["gen:plan-week"]).items.count, 2)
    }

    func testDerivedCurrentExchangeFollowsThePluginsRule() {
        let t0 = Date(timeIntervalSince1970: 1_800_000_000)
        func msg(_ id: String, _ role: NowStream.Entry.Role, _ s: TimeInterval, run: String? = nil) -> NowStream.Entry {
            NowStream.Entry(id: id, kind: .message, at: t0.addingTimeInterval(s), text: "hi", role: role, runID: run)
        }
        var answerLine = msg("d:run_old", .assistant, 30, run: "run_old"); answerLine.answer = true
        var result = NowStream.Entry(id: "x:run_a", kind: .result, at: t0.addingTimeInterval(40), itemID: "s:a", runID: "run_a")
        result.cardID = "c:run_a"
        let entries = [msg("m:hu_0", .user, -600), msg("m:ha_0", .assistant, -599),
                       msg("m:hu_1", .user, 0, run: "run_a"), msg("m:ha_1", .assistant, 5),
                       NowStream.Entry(id: "c:run_a", kind: .card, at: t0.addingTimeInterval(5), itemID: "s:a"),
                       NowStream.Entry(id: "c:run_old", kind: .card, at: t0.addingTimeInterval(-500), itemID: "s:o"),
                       answerLine, result]
        let rule = HomeCurrent.resetAfter
        let cur = HomeCurrent.derive(from: entries, now: t0.addingTimeInterval(60), resetAfter: rule)
        XCTAssertFalse(cur.clean)
        XCTAssertEqual(cur.entryIDs, ["m:hu_1", "m:ha_1", "c:run_a", "x:run_a"])
        XCTAssertEqual(cur.messageID, "m:hu_1")
        XCTAssertEqual(cur.replyIDs, ["m:ha_1"], "an older job's answer line isn't a reply in this exchange")
        XCTAssertEqual(cur.runIDs, ["run_a"])
        XCTAssertEqual(cur.resetsAt, t0.addingTimeInterval(5 + HomeCurrent.resetAfter))
        // 20 minutes after the last message: clean (a later result doesn't count as activity).
        let late = HomeCurrent.derive(from: entries, now: t0.addingTimeInterval(5 + rule), resetAfter: rule)
        XCTAssertTrue(late.clean)
        XCTAssertEqual(late.entryIDs, [])
        XCTAssertEqual(late.lastActivityAt, t0.addingTimeInterval(5))
        XCTAssertTrue(HomeCurrent.derive(from: [], now: t0).clean)
        // An older plugin has no reset (D1): the app's fallback keeps the exchange however long it's been.
        let stream = NowStream(entries: entries)
        let old = stream.currentExchange(now: t0.addingTimeInterval(86_400))
        XCTAssertFalse(old.clean)
        XCTAssertNil(old.resetsAt)
        XCTAssertEqual(old.entryIDs, cur.entryIDs)
        let item = NowItem(id: "s:a", kind: .newAnswer, runID: "run_a", title: "Find a lamp", line: "Three lamps",
                           finishedAt: t0.addingTimeInterval(40), unread: true)
        let stack = HomeStack.derive(from: [item], stream: stream, currentRunIDs: Set(cur.runIDs))
        XCTAssertEqual(stack.items.first?.resultID, nil, "the card entry carries no result id here")
        XCTAssertEqual(stack.items.first?.inCurrent, true)
    }
}
