import XCTest
@testable import SpeakeasyCore

/// Home (SPK-124): the app decodes the plugin's real /voice/now reply (Contract/now.json, written by
/// plugin/tests/test_now.py from invented data) and draws it in the plugin's order.
final class NowHomeTests: XCTestCase {
    private func contract() throws -> [String: Any] {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "now", withExtension: "json", subdirectory: "Contract"))
        return try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    }

    func testDecodesThePluginsReplyInItsOrder() throws {
        let home = NowHome(json: try contract())
        XCTAssertEqual(home.items.map(\.kind), [.question, .stopped, .working, .newAnswer])
        XCTAssertEqual(home.sentence, "One question for you, one thing stopped, one thing in the works, and one new answer.")
        XCTAssertEqual(home.counts.needsYou, 1)
        XCTAssertEqual(home.counts.finishedUnread, 1)
        let q = try XCTUnwrap(home.items.first?.question)
        XCTAssertEqual(q.shape, .things)
        XCTAssertEqual(q.options.map(\.id), ["o1", "o2"])
        XCTAssertEqual(q.options.first?.link?.absoluteString, "https://desks.example/oakline")
        XCTAssertEqual(q.options.first?.facts, ["$389", "60 in. wide"])
        XCTAssertNil(q.options.last?.link, "only https links")
        XCTAssertTrue(q.comparable)
        XCTAssertEqual(home.items[1].fix, .credits)
        XCTAssertTrue(home.items[3].unread)
    }

    func testCapsAndOverflowWords() {
        XCTAssertEqual(NowCaps.small.cap(.question), 1)
        XCTAssertEqual(NowCaps.phone.cap(.working), 3)
        XCTAssertEqual(NowCaps.overflowLabel(.question, 2), "2 more questions")
        XCTAssertEqual(NowCaps.overflowLabel(.working, 3), "3 more in the works")
        XCTAssertEqual(NowCaps.overflowLabel(.newAnswer, 1), "1 more new answer")
    }

    func testEarlierKindsQueryAndMenu() {
        XCTAssertEqual(EarlierKind.query([]), "conversations")
        XCTAssertEqual(EarlierKind.query(["archived", "kanban", "conversations"]), "conversations,kanban,archived")
        let menu = EarlierKind.parse([["id": "conversations", "label": "Conversations", "count": 12, "on": true],
                                      ["id": "nope", "count": 1], ["id": "subagents", "count": 3, "on": false]])
        XCTAssertEqual(menu.map(\.id), ["conversations", "subagents"])
        XCTAssertEqual(menu.last?.label, "Subagents")
    }

    func testProvenanceLine() {
        var cal = Calendar(identifier: .gregorian); cal.timeZone = TimeZone(identifier: "America/New_York")!
        let at = Date(timeIntervalSince1970: 1_794_000_000)  // invented
        let item = NowItem(id: "s:x", kind: .newAnswer, title: "Desk hunt", line: "Done.", provenance: .yesterday(at))
        let nextDay = at.addingTimeInterval(86400)
        XCTAssertTrue(item.provenanceLine(calendar: cal, now: nextDay)?.hasPrefix("From yesterday · ") == true)
        XCTAssertFalse(item.provenanceLine(calendar: cal, now: nextDay)?.contains("so far") == true)
    }

    func testCallFocusOnCallMarksAndTheCallSummaryDecode() {
        let home = NowHome(json: [
            "rev": 3, "sentence": "Two things in the works.",
            "call": ["interaction_id": "vi_1", "started_at": 1_800_000_000, "focus": "s:s_w", "focus_label": "Talking about this"],
            "items": [["id": "q:run_q", "kind": "question", "title": "Desk hunt", "on_call": "asking", "say_ok": false,
                       "lock_line": "Can't be undone · Needs a tap"],
                      ["id": "s:s_h", "kind": "seen_answer", "title": "Tire shop", "on_call": "heard", "line": "Told you on the call."]],
        ])
        XCTAssertEqual(home.call?.focus, "s:s_w")
        XCTAssertEqual(home.call?.focusLabel, "Talking about this")
        XCTAssertEqual(home.items[0].onCall, .asking)
        XCTAssertFalse(home.items[0].sayOK)
        XCTAssertEqual(home.items[0].lockLine, "Can't be undone · Needs a tap")
        XCTAssertEqual(home.items[1].onCall, .heard)
        XCTAssertNil(home.callSummary)
        let after = NowHome(json: ["call_summary": ["id": "call:vi_1", "title": "Your call · 6 min", "minutes": 6,
                                                    "ended_at": 1_800_000_400, "things": ["Desk hunt", "Saturday plan"],
                                                    "decisions": ["The wider desk"]]])
        XCTAssertNil(after.call)
        XCTAssertEqual(after.callSummary?.minutes, 6)
        XCTAssertEqual(after.callSummary?.started, "Started Desk hunt and Saturday plan.")
        XCTAssertEqual(after.callSummary?.youSaid, "The wider desk")
    }

    func testCallSummaryUsesThePluginsLineAndNeverSaysTalkedItThrough() {
        let bare = NowCallSummary(json: ["id": "call:1", "ended_at": 1_800_000_000, "minutes": 1])!
        XCTAssertEqual(bare.summaryLine, "Short call, nothing started.")
        let lined = NowCallSummary(json: ["id": "call:1", "ended_at": 1_800_000_000, "things": ["Desk hunt"],
                                          "line": "Started Desk hunt. Answered Seed list."])!
        XCTAssertEqual(lined.summaryLine, "Started Desk hunt. Answered Seed list.")
    }

    func testProvenanceSaysYesterdayOnlyForRealYesterday() {
        var cal = Calendar(identifier: .gregorian); cal.timeZone = TimeZone(identifier: "America/New_York")!
        let now = cal.date(from: DateComponents(year: 2026, month: 11, day: 6, hour: 22, minute: 23))!
        func line(_ day: Int, _ kind: String) -> String? {
            let at = cal.date(from: DateComponents(year: 2026, month: 11, day: day, hour: 20, minute: 13))!
            let item = NowItem(json: ["id": "s:x", "kind": "new_answer", "title": "T",
                                      "provenance": ["kind": kind, "at": at.timeIntervalSince1970]])!
            return item.provenanceLine(calendar: cal, now: now)
        }
        XCTAssertEqual(line(5, "yesterday"), "From yesterday \u{00B7} 8:13 PM")
        XCTAssertEqual(line(4, "yesterday"), "From Wednesday \u{00B7} 8:13 PM", "an old plugin's 'yesterday' is checked")
        XCTAssertEqual(line(4, "earlier_day"), "From Wednesday \u{00B7} 8:13 PM")
        let old = cal.date(from: DateComponents(year: 2026, month: 10, day: 28, hour: 9))!
        let item = NowItem(json: ["id": "s:x", "kind": "new_answer", "title": "T",
                                  "provenance": ["kind": "earlier_day", "at": old.timeIntervalSince1970]])!
        XCTAssertEqual(item.provenanceLine(calendar: cal, now: now), "From Oct 28")
    }

    func testCaughtUpAndClearable() {
        XCTAssertTrue(NowHome(json: ["items": []]).caughtUp)
        let h = NowHome(json: ["items": [["id": "s:a", "kind": "new_answer", "title": "A"],
                                         ["id": "s:b", "kind": "working", "title": "B"]]])
        XCTAssertFalse(h.caughtUp)
        XCTAssertEqual(h.clearable.map(\.id), ["s:a"])
    }

    // SPK-208: Home as one stream.
    func testDecodesTheStreamAsAsksAndCardsInTimeOrder() throws {
        let home = NowHome(json: try contract())
        let stream = try XCTUnwrap(home.stream)
        XCTAssertEqual(stream.entries.map(\.at), stream.entries.map(\.at).sorted())
        let ids = Set(home.items.map(\.id))
        for entry in stream.entries where entry.kind != .group { XCTAssertTrue(ids.contains(try XCTUnwrap(entry.itemID))) }
        let asks = stream.entries.filter { $0.kind == .ask }
        XCTAssertEqual(asks.first?.text, "Desk hunt")
        let question = try XCTUnwrap(stream.entries.first { $0.kind == .card && $0.itemID == home.items.first?.id })
        XCTAssertEqual(question.widget, .open)
        XCTAssertEqual(stream.needsYou, [try XCTUnwrap(home.items.first?.id)])
    }

    // Real-data fixes: short title + whole title, picture captions/hero crops, replaced questions, the pill.
    func testRealDataFieldsDecode() throws {
        let home = NowHome(json: [
            "pill": "3 need you \u{00B7} 5 running",
            "items": [
                ["id": "s:a", "kind": "new_answer", "title": "Fix picture sending",
                 "full_title": "Fix picture sending and carousels in the shared thread after the last update",
                 "thumbs": [["url": "/voice/card-image/run_a/1", "caption": "Before and after", "aspect": 4.2,
                             "hero": "/voice/card-image/run_a/1?v=hero"],
                            ["url": "/voice/card-image/run_a/3"]]],
                ["id": "q:b", "kind": "waiting", "title": "Pick a dinner spot", "line": "Replaced by a newer question.",
                 "replaced": ["at": 1_000, "by": "q:c"]],
            ],
            "stream": ["entries": [["id": "c:b", "kind": "card", "at": 900, "item_id": "q:b", "widget": "replaced",
                                    "replaced_at": 1_000]], "needs_you": []],
        ])
        XCTAssertEqual(home.pill, "3 need you \u{00B7} 5 running")
        let a = home.items[0]
        XCTAssertEqual(a.title, "Fix picture sending")
        XCTAssertEqual(a.fullTitle, "Fix picture sending and carousels in the shared thread after the last update")
        XCTAssertEqual(a.thumbs, ["/voice/card-image/run_a/1", "/voice/card-image/run_a/3"])
        XCTAssertEqual(a.pictures.map(\.caption), ["Before and after", nil])
        XCTAssertEqual(a.pictures.map(\.number), [1, 3])
        XCTAssertEqual(a.pictures.first?.hero, "/voice/card-image/run_a/1?v=hero")
        XCTAssertNil(a.replacedAt)
        let b = home.items[1]
        XCTAssertEqual(b.replacedAt, Date(timeIntervalSince1970: 1_000))
        XCTAssertEqual(b.replacedBy, "q:c")
        let entry = try XCTUnwrap(home.stream?.entries.first)
        XCTAssertEqual(entry.widget, .replaced)
        XCTAssertEqual(entry.replacedAt, Date(timeIntervalSince1970: 1_000))
        // An older plugin: no pill, url-only thumbs, no full title.
        let old = NowHome(json: ["items": [["id": "s:x", "kind": "working", "title": "T",
                                            "thumbs": [["url": "/voice/card-image/r/2"]]]]])
        XCTAssertNil(old.pill)
        XCTAssertNil(old.items[0].fullTitle)
        XCTAssertEqual(old.items[0].pictures, [NowPicture(url: "/voice/card-image/r/2")])
    }

    // SPK-220: a message sent with a clip carries it (and may have no words).
    func testAMessageCarriesItsFilesAndMayHaveNoWords() throws {
        let stream = try XCTUnwrap(NowStream(json: ["entries": [
            ["id": "m:hu_1", "kind": "message", "role": "user", "text": "", "at": 100, "client_id": "c1",
             "files": [["id": "att_1", "kind": "video", "mime": "video/mp4", "name": "porch.mp4", "duration": 12.2],
                       ["id": "att_2", "kind": "picture", "mime": "image/jpeg"],
                       ["id": "att_3", "mime": "application/pdf", "name": "lease.pdf"],
                       ["kind": "video"]]],
            ["id": "m:hu_2", "kind": "message", "role": "user", "text": "", "at": 101],
            ["id": "m:hu_3", "kind": "message", "role": "user", "text": "look", "at": 102],
        ], "needs_you": []]))
        XCTAssertEqual(stream.entries.map(\.id), ["m:hu_1", "m:hu_3"])   // no words and no files: dropped
        let m = stream.entries[0]
        XCTAssertNil(m.text)
        XCTAssertEqual(m.clientID, "c1")
        XCTAssertEqual(m.files, [SentFile(id: "att_1", kind: .video, name: "porch.mp4", mime: "video/mp4", duration: 12.2),
                                 SentFile(id: "att_2", kind: .picture, mime: "image/jpeg"),
                                 SentFile(id: "att_3", kind: .file, name: "lease.pdf", mime: "application/pdf")])
        XCTAssertEqual(m.files.map(\.label), ["Video \u{00B7} 0:12", "Photo", "lease.pdf"])
        XCTAssertEqual(m.files.map(\.spoken), ["Video, 12 seconds", "Photo", "File, lease.pdf"])
        XCTAssertEqual(m.files.map(\.symbol), ["video.fill", "photo", "doc"])
        XCTAssertEqual(stream.entries[1].files, [])
        XCTAssertEqual(SentFile.clock(65), "1:05")
        XCTAssertEqual(SentFile(id: "v", kind: .video, duration: 61).spoken, "Video, 1 minute 1 second")
    }

    // SPK-314: a job that finished after a newer message says its answer at the bottom, tied to its card.
    func testAJobsAnswerLineDecodesAsAReplyTiedToItsCard() throws {
        let stream = try XCTUnwrap(NowStream(json: ["entries": [
            ["id": "c:r1", "kind": "card", "at": 100, "item_id": "s:cafe", "state": "new_answer"],
            ["id": "m:hu_2", "kind": "message", "role": "user", "text": "how far is the moon?", "at": 120],
            ["id": "d:r1", "kind": "message", "role": "assistant", "text": "Riverside Roasters opens at 7.", "at": 150,
             "run_id": "r1", "item_id": "s:cafe", "answer": true],
        ], "needs_you": []]))
        let line = try XCTUnwrap(stream.entries.last)
        XCTAssertEqual(line.kind, .message)
        XCTAssertEqual(line.role, .assistant)
        XCTAssertEqual(line.itemID, "s:cafe")
        XCTAssertEqual(line.runID, "r1")
        XCTAssertEqual(line.text, "Riverside Roasters opens at 7.")
        XCTAssertNil(stream.entries[1].itemID, "a chat message is not tied to a card")
    }

    func testAnOlderPluginHasNoStreamAndAGroupDecodes() {
        XCTAssertNil(NowHome(json: ["rev": 1]).stream)
        let group = NowStream(json: ["entries": [["id": "g:morning", "kind": "group", "at": 100, "line": "This morning \u{00B7} 6 done",
                                                  "item_ids": ["a", "b"]]], "needs_you": []])
        XCTAssertEqual(group?.entries.first?.line, "This morning \u{00B7} 6 done")
        XCTAssertEqual(group?.entries.first?.itemIDs, ["a", "b"])
        XCTAssertNil(NowStream.Entry(json: ["id": "x", "kind": "mystery", "at": 1]), "an unknown kind is dropped, not shown")
    }

    // b73: a drafted email posts as its own entry at the bottom; the job's card links down to it.
    func testAPostedResultAndItsCardLinkEachOther() throws {
        let stream = try XCTUnwrap(NowStream(json: ["entries": [
            ["id": "c:run_a", "kind": "card", "at": 100, "item_id": "n:a", "result_id": "x:run_a"],
            ["id": "m:hu_1", "kind": "message", "role": "user", "text": "thanks", "at": 150],
            ["id": "x:run_a", "kind": "result", "at": 200, "item_id": "n:a", "run_id": "run_a", "card_id": "c:run_a"],
            ["id": "x:run_b", "kind": "result", "at": 210],   // no job behind it: dropped
        ], "needs_you": []]))
        XCTAssertEqual(stream.entries.map(\.id), ["c:run_a", "m:hu_1", "x:run_a"])
        XCTAssertEqual(stream.entries[0].resultID, "x:run_a")
        XCTAssertEqual(stream.entries[2].kind, .result)
        XCTAssertEqual(stream.entries[2].cardID, "c:run_a")
        XCTAssertEqual(stream.entries[2].runID, "run_a")
    }

    func testRecentRowsSayDoneStoppedOrInProgress() throws {
        let rows = ["completed", "failed", "running"].compactMap {
            NowRecentItem(json: ["id": "t:\($0)", "title": "Plan the trip", "state": $0, "at": 5])
        }
        XCTAssertEqual(rows.map(\.statusWords), ["Done", "Stopped", "In progress"])
    }
}
