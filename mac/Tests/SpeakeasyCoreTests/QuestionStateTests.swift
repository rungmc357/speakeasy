import XCTest
@testable import SpeakeasyCore

/// SPK-79: the server owns a question's state; the app keeps only picks the server hasn't confirmed.
final class QuestionStateTests: XCTestCase {
    let now = Date()

    func asking(_ id: String, state: String? = nil, answer: String? = nil, age: TimeInterval = 10,
                extra: [String: Any] = [:]) -> TaskItem {
        var json: [String: Any] = ["task_id": id, "run_id": "run_\(id)", "status": "completed",
                                   "updated_at": now.timeIntervalSince1970 - age,
                                   "result": ["views": [["kind": "question", "question": "Paint the shed green or grey?",
                                                         "options": ["Green", "Grey"]]]]]
        if let state { json["question_state"] = state }
        if let answer { json["question_answer"] = answer }
        json.merge(extra) { _, new in new }
        return TaskItem(json: json)!
    }

    // MARK: Decoding

    func testQuestionStateDecodesAndOlderFieldsAreDerived() {
        XCTAssertEqual(asking("a", state: "superseded").info.questionStateOrDerived, .superseded)
        XCTAssertEqual(asking("b", state: "answered", answer: "Grey").info.questionAnswer, "Grey")
        XCTAssertEqual(asking("c").info.questionStateOrDerived, .open, "an older plugin: nothing closed it")
        XCTAssertEqual(asking("d", extra: ["answered": true]).info.questionStateOrDerived, .answered)
        XCTAssertEqual(asking("e", extra: ["question_closed": "moved_on", "set_aside": true]).info.questionStateOrDerived,
                       .movedOn)
        XCTAssertEqual(asking("f", extra: ["set_aside": true]).info.questionStateOrDerived, .setAside)
        XCTAssertNil(TaskItem(json: ["task_id": "g", "status": "completed"])!.info.questionStateOrDerived,
                     "a task that asks nothing has no question state")
    }

    // MARK: The board reads the server's state alone

    func testOnlyAnOpenQuestionNeedsYou() {
        for state in ["answered", "set_aside", "dismissed", "moved_on", "expired", "superseded"] {
            let jobs = JobBoard.jobs([asking("q", state: state)], now: now)
            XCTAssertNotEqual(jobs.first?.stage, .needsYou(.decide), state)
        }
        XCTAssertEqual(JobBoard.jobs([asking("q", state: "open")], now: now).first?.stage, .needsYou(.decide))
    }

    func testTheServerExpiresQuestionsNotTheClientClock() {
        // A plugin with question_state closes a day-old question itself (expired); until it says so the
        // card stays (a Mac whose clock is ahead must not drop an open decision).
        let old = asking("q", state: "open", age: 30 * 3600)
        XCTAssertEqual(JobBoard.jobs([old], now: now).first?.stage, .needsYou(.decide))
        // An older plugin still relies on the client's 24 h rule.
        XCTAssertNotEqual(JobBoard.jobs([asking("q", age: 30 * 3600)], now: now).first?.stage, .needsYou(.decide))
    }

    // MARK: The pending-pick store

    func testReconcileDropsWhatTheServerSettledAndKeepsTheRest() {
        var store = PickStore()
        store.set("run_a", .init(kind: .answer, text: "Green"))
        store.set("run_b", .init(kind: .dismissed))
        store.set("run_c", .init(kind: .answer, text: "Grey"))
        let states: [String: QuestionState] = ["run_a": .answered, "run_b": .open]
        store.reconcile { states[$0] }
        XCTAssertNil(store["run_a"], "confirmed: the server's state shows it from now on")
        XCTAssertEqual(store["run_b"]?.kind, .dismissed, "not confirmed yet: still hidden here")
        XCTAssertEqual(store.text("run_c"), "Grey", "a task not loaded yet keeps its pick")
    }

    func testAnUnconfirmedPickDoesNotHideTheCardForever() {
        var store = PickStore()
        store.set("run_a", .init(kind: .answer, text: "Green", at: now.addingTimeInterval(-PickStore.maxAge - 5)))
        store.reconcile(now: now) { _ in .open }
        XCTAssertNil(store["run_a"])
    }

    func testEvictionDropsTheOldestNotArbitraryEntries() {
        var store = PickStore()
        for i in 0..<(PickStore.maxEntries + 20) {
            store.set("run_\(i)", .init(kind: .answer, text: "x", at: now.addingTimeInterval(Double(i) - 600)))
        }
        XCTAssertEqual(store.entries.count, PickStore.maxEntries)
        XCTAssertNil(store["run_0"], "the oldest goes first")
        XCTAssertNotNil(store["run_\(PickStore.maxEntries + 19)"], "the newest pick is always kept")
    }

    func testPicksSurviveARelaunchUntilTheServerConfirms() {
        let defaults = UserDefaults(suiteName: "spk79-\(UUID().uuidString)")!
        var store = PickStore()
        store.set("run_a", .init(kind: .dismissed))
        store.save(defaults, key: "picks")
        var again = PickStore.load(defaults, key: "picks")
        XCTAssertEqual(again["run_a"]?.kind, .dismissed)
        again.reconcile { _ in .dismissed }
        XCTAssertTrue(again.entries.isEmpty, "after the server confirms, only its state decides")
    }

    func testTheSamePickAlwaysSendsUnderTheSameClientID() {
        let a = PickStore.clientID(questionID: "run_a", text: "Green")
        XCTAssertEqual(a, PickStore.clientID(questionID: "run_a", text: "green"))
        XCTAssertNotEqual(a, PickStore.clientID(questionID: "run_a", text: "Grey"))
        XCTAssertNotEqual(a, PickStore.clientID(questionID: "run_b", text: "Green"))
        XCTAssertNotNil(a.range(of: "^[A-Za-z0-9_-]{1,128}$", options: .regularExpression), "a server id")
    }

    func testABoardAnswerWithNoCallGoesByRunID() {
        // The verified bug: with no call open the board marked the pick answered and sent nothing.
        XCTAssertEqual(QuestionAnswerRoute.choose(callOpen: false, interactionID: nil, runID: "run_a",
                                                  questionState: true, taskMessages: true), .byRun("run_a"))
        XCTAssertEqual(QuestionAnswerRoute.choose(callOpen: false, interactionID: "call_old", runID: "run_a",
                                                  questionState: true, taskMessages: true), .byRun("run_a"),
                       "a closed call's id is not where an answer goes")
        XCTAssertEqual(QuestionAnswerRoute.choose(callOpen: false, interactionID: nil, runID: "run_a",
                                                  questionState: false, taskMessages: true), .byMessage("run_a"))
        XCTAssertEqual(QuestionAnswerRoute.choose(callOpen: true, interactionID: "call_1", runID: "run_a",
                                                  questionState: true, taskMessages: true), .call("call_1"),
                       "during a call the voice hears the answer")
        XCTAssertEqual(QuestionAnswerRoute.choose(callOpen: false, interactionID: nil, runID: "run_a",
                                                  questionState: false, taskMessages: false), .none)
    }
}
