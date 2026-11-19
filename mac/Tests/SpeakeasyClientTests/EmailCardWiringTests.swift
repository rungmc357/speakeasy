import XCTest
@testable import SpeakeasyClient
@testable import SpeakeasyCore

/// SPK-364: what the email card's end rests on outside SpeakeasyCore. The card's Send awaits
/// `runCardAction`'s `done`, so every path answers it once; `EmailCardMemory` then settles the
/// phase from that answer, and a newer tap on the draft keeps its own.
final class EmailCardWiringTests: XCTestCase {
    private final class Log: @unchecked Sendable {
        var answers: [Bool] = []
        var replies: [(Bool, String) -> Void] = []
        var posts = 0
        var undonePosts = 0
        var held: CheckedContinuation<Bool, Never>?
        var lateAnswers = 0
    }

    private let card = ViewCard(kind: "draft", data: ["channel": "email"])
    private let send = CardAction(id: "send", label: "Send")

    /// Lets the memory's own tasks run until `done` holds (five seconds at most).
    @MainActor private func wait(until done: () -> Bool) async {
        var tries = 0
        while !done(), tries < 500 {
            try? await Task.sleep(for: .milliseconds(10))
            tries += 1
        }
    }

    // MARK: VoicePanelModel.runCardAction

    @MainActor func testACardActionAnswersOnceWhetherHermesTakesItOrNot() {
        let model = VoicePanelModel(), log = Log()
        model.onCardAction = { _, _, _, reply in reply(true, "Sending.") }
        model.runCardAction(taskID: "t1", card: card, action: send) { log.answers.append($0) }
        XCTAssertEqual(log.answers, [true])
        model.onCardAction = { _, _, _, reply in reply(false, "draft changed; review the current draft") }
        model.runCardAction(taskID: "t1", card: card, action: send) { log.answers.append($0) }
        XCTAssertEqual(log.answers, [true, false])
        XCTAssertTrue(model.cardActionBusy.isEmpty, "the button is free again either way")
    }

    @MainActor func testAServerWithoutCardButtonsAnswersFalse() {
        let model = VoicePanelModel(), log = Log()
        model.runCardAction(taskID: "t1", card: card, action: send) { log.answers.append($0) }
        XCTAssertEqual(log.answers, [false])
    }

    @MainActor func testATapWhileTheSameButtonIsOutAnswersFalseAtOnce() {
        let model = VoicePanelModel(), first = Log(), second = Log()
        model.onCardAction = { _, _, _, reply in first.replies.append(reply) }
        model.runCardAction(taskID: "t1", card: card, action: send) { first.answers.append($0) }
        model.runCardAction(taskID: "t1", card: card, action: send) { second.answers.append($0) }
        XCTAssertEqual(first.replies.count, 1, "the second tap never posts")
        XCTAssertEqual(second.answers, [false], "and never leaves its caller waiting")
        XCTAssertTrue(first.answers.isEmpty)
        first.replies.first?(true, "Sending.")
        XCTAssertEqual(first.answers, [true])
        XCTAssertEqual(second.answers, [false])
        XCTAssertTrue(model.cardActionBusy.isEmpty)
    }

    // MARK: EmailCardMemory

    @MainActor func testTheAnswerSettlesThePhase() async {
        let memory = EmailCardMemory(), log = Log()
        memory.ask("refused", "send") { log.posts += 1; return false }
        memory.ask("saved", "save") { log.posts += 1; return true }
        memory.ask("taken", "send") { log.posts += 1; return true }
        XCTAssertEqual(memory.phase["refused"], .asked("send"), "Sending… while the POST is out")
        await wait { log.posts == 3 }
        XCTAssertNil(memory.phase["refused"], "a refused Send gives the buttons back")
        XCTAssertNil(memory.phase["saved"], "a saved draft is a draft again")
        XCTAssertEqual(memory.phase["taken"], .asked("send"), "a Send Hermes took waits on its status")
    }

    @MainActor func testALateAnswerLeavesANewerTapAlone() async {
        let window = EmailCardMemory.undoSeconds
        EmailCardMemory.undoSeconds = 60
        defer { EmailCardMemory.undoSeconds = window }
        let memory = EmailCardMemory(), log = Log()
        memory.ask("d1", "save") {
            let ok: Bool = await withCheckedContinuation { log.held = $0 }
            log.lateAnswers += 1
            return ok
        }
        await wait { log.held != nil }
        memory.schedule("d1", verb: "send") { log.posts += 1; return true }
        let undo = memory.phase["d1"]
        guard case .waiting("send", until: _)? = undo else { return XCTFail("Send's Undo window owns the phase") }
        log.held?.resume(returning: true)
        await wait { log.lateAnswers == 1 }
        XCTAssertEqual(memory.phase["d1"], undo, "Save's late answer never ends Send's Undo window")
        memory.undo("d1")
        XCTAssertNil(memory.phase["d1"])
        XCTAssertEqual(log.posts, 0, "an undone Send never posts")
    }

    @MainActor func testAfterTheUndoWindowTheAskPostsAndSettles() async {
        let window = EmailCardMemory.undoSeconds
        EmailCardMemory.undoSeconds = 0.05
        defer { EmailCardMemory.undoSeconds = window }
        let memory = EmailCardMemory(), log = Log()
        memory.schedule("refused", verb: "send") { log.posts += 1; return false }
        memory.schedule("taken", verb: "discard") { log.posts += 1; return true }
        memory.schedule("undone", verb: "send") { log.undonePosts += 1; return true }
        memory.undo("undone")
        guard case .waiting("send", until: _)? = memory.phase["refused"] else { return XCTFail("the Undo window comes first") }
        await wait { log.posts == 2 }
        try? await Task.sleep(for: .milliseconds(100))
        XCTAssertNil(memory.phase["refused"], "a Send refused after its window gives the buttons back")
        XCTAssertEqual(memory.phase["taken"], .asked("discard"))
        XCTAssertNil(memory.phase["undone"])
        XCTAssertEqual(log.undonePosts, 0, "an undone Send never posts")
    }
}
