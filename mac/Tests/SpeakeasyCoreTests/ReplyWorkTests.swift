import XCTest
@testable import SpeakeasyCore

final class ReplyWorkTests: XCTestCase {
    private func tool(_ id: String, failed: Bool = false, at: Double = 10) -> JobActivityItem {
        .init(id: id, at: Date(timeIntervalSince1970: at), kind: .run, text: "Ran a command",
              status: failed ? .failed : .done, tool: "terminal", args: "printf sample", result: "sample")
    }
    private var exchange: [HistoryEntry] {
        [.init(id: "u", kind: .user, text: "Check the build"),
         .init(id: "n1", kind: .assistant, text: "Checking the test report"),
         .init(id: "t1", kind: .tools, tools: [tool("1", failed: true)]),
         .init(id: "n2", kind: .assistant, text: "Reading the saved result"),
         .init(id: "t2", kind: .tools, tools: [tool("1")]),
         .init(id: "a", kind: .assistant, text: "The build passed.\n```sh\nprintf sample\n```")]
    }
    func testNarrationAndBatchesBecomeOneCardAboveUnchangedAnswer() throws {
        let rows = ReplyWorkLayout.rows(exchange)
        XCTAssertEqual(rows.map(\.id), ["u", "work-n1", "a"])
        let work = try XCTUnwrap(rows[1].work)
        XCTAssertEqual(work.sections.map(\.heading), ["Checking the test report", "Reading the saved result"])
        XCTAssertEqual(work.calls.count, 2)
        XCTAssertEqual(Set(work.calls.map(\.id)).count, 2)
        XCTAssertEqual(rows.last?.entry?.text, exchange.last?.text)
        XCTAssertFalse(work.running)
        XCTAssertFalse(work.failed, "A recovered lookup must not warn on the collapsed card")
        XCTAssertEqual(work.calls.first?.status, .failed)
        XCTAssertEqual(work.calls.last?.args, "printf sample")
        XCTAssertEqual(work.calls.last?.result, "sample")
    }
    func testDistinctRepliesAndUserTurnsNeverMerge() {
        let rows = ReplyWorkLayout.rows(exchange + exchange.map { var e = $0; e.id += "-2"; return e })
        XCTAssertEqual(rows.filter { $0.work != nil }.count, 2)
        XCTAssertEqual(rows.filter { $0.entry?.kind == .assistant }.count, 2)
    }
    func testAnswerWithoutToolsRemainsAnAnswer() {
        let e = HistoryEntry(id: "a", kind: .assistant, text: "Checking is complete.")
        XCTAssertEqual(ReplyWorkLayout.rows([e]).first?.entry, e)
    }
    func testPartialPageWithoutNarrationStillHasOneCard() throws {
        let rows = ReplyWorkLayout.rows(Array(exchange.dropFirst(2)))
        XCTAssertEqual(rows.filter { $0.work != nil }.count, 1)
        XCTAssertEqual(try XCTUnwrap(rows.first?.work).calls.count, 2)
    }
    func testLiveIdentitySurvivesAnswerArrivalAndUsesOnlyNewestHeading() throws {
        let live = ReplyWorkLayout.rows(Array(exchange.dropLast()), running: true)
        let done = ReplyWorkLayout.rows(exchange)
        let work = try XCTUnwrap(live.last?.work)
        XCTAssertTrue(work.running)
        XCTAssertEqual(work.id, done[1].work?.id)
        XCTAssertEqual(work.line, "Reading the saved result")
        XCTAssertFalse(try XCTUnwrap(done[1].work).running)
    }
    func testFailedReplyNotRecoveredToolControlsWarning() throws {
        let rows = ReplyWorkLayout.rows(Array(exchange.dropLast()), failed: true)
        XCTAssertTrue(try XCTUnwrap(rows.last?.work).failed)
    }
    func testNewTaskIsNotHiddenByOldRecordedWork() throws {
        var job = try XCTUnwrap(WorkInfo(json: ["run_id": "new", "status": "running", "started_at": 20]))
        job.sessionID = "s"
        let c = HistoryConversation(id: "s", title: "Build report", source: "discord", sourceLabel: "Discord", entries: exchange, jobs: [job])
        XCTAssertFalse(ReplyWorkLayout.represents(job, in: c))
        job.startedAt = Date(timeIntervalSince1970: 5)
        XCTAssertTrue(ReplyWorkLayout.represents(job, in: c))
    }
    func testStatusOnlyNotesFoldEvenWithoutAdjacentToolRows() throws {
        let note = try XCTUnwrap(HistoryEntry(json: ["id": "s", "kind": "assistant", "text": "Checking the report",
                                                   "narration": true, "work_detail": "Reading the saved log"]))
        let answer = HistoryEntry(id: "a", kind: .assistant, text: "The report passed.")
        let rows = ReplyWorkLayout.rows([note, answer])
        XCTAssertEqual(rows.count, 2)
        let work = try XCTUnwrap(rows.first?.work)
        XCTAssertEqual(work.sections.first?.detail, "Reading the saved log")
        XCTAssertEqual(rows.last?.entry, answer)
    }
    func testModelLabelStaysOnAnswerNotFoldedNarration() {
        let author = ReplyAttribution(model: ModelBadge(id: "sample-fast", name: "Sample Fast", providerName: "Sample"))
        var entries = exchange
        entries[1].attribution = author; entries[3].attribution = author; entries[5].attribution = author
        let c = HistoryConversation(id: "s", title: "Build report", source: "discord", sourceLabel: "Discord", entries: entries)
        XCTAssertEqual(c.replyModelLabels, ["a"])
    }
    func testOneSentenceDoesNotGlueDetail() {
        XCTAssertEqual(ReplyWork.sentence("Checking the report — reading its log"), "Checking the report")
    }
}
