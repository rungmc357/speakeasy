import XCTest
@testable import SpeakeasyCore

/// A board card's decision buttons: the recommended answer as the button in its own words, a quieter
/// "Other options", and nothing that only opens options already on screen (iPhone Duo open, iPad).
final class DecisionButtonsTests: XCTestCase {
    private func decision(_ options: [String], recommended: Int? = 0, approvable: Bool = true) -> Decision {
        var data: [String: Any] = ["question": "Send the weekly report now?", "options": options, "approvable": approvable]
        if let recommended { data["recommended"] = recommended }
        return Decision(card: ViewCard(kind: "question", data: data))!
    }

    func testRecommendedAnswerIsTheButtonWithQuieterOtherOptions() {
        let b = decision(["Ship it", "Wait until Monday", "Drop it"]).boardButtons(optionsShowing: false)
        XCTAssertEqual(b, DecisionButtons(pick: "Ship it", more: "Other options"))
    }

    func testOptionsAlreadyShowingBesideTheBoardHidesTheWayToThem() {
        let d = decision(["Ship it", "Wait until Monday"])
        XCTAssertEqual(d.boardButtons(optionsShowing: true), DecisionButtons(pick: "Ship it", more: nil))
        let noTap = decision(["Ship it", "Wait until Monday"], approvable: false)
        XCTAssertEqual(noTap.boardButtons(optionsShowing: true), DecisionButtons(pick: nil, more: nil))
    }

    func testWithoutOneTapApprovalTheCardOnlyOpensTheOptions() {
        // The plugin didn't allow one tap: never inferred from a recommendation alone.
        XCTAssertEqual(decision(["Ship it", "Wait"], approvable: false).boardButtons(optionsShowing: false),
                       DecisionButtons(pick: nil, more: "See options"))
        // No recommendation: approvable is dropped, so there's nothing to answer with in one tap.
        XCTAssertEqual(decision(["Ship it", "Wait"], recommended: nil).boardButtons(optionsShowing: false),
                       DecisionButtons(pick: nil, more: "See options"))
    }

    func testASingleOptionNeedsNoOtherOptions() {
        XCTAssertEqual(decision(["Ship it"]).boardButtons(optionsShowing: false), DecisionButtons(pick: "Ship it", more: nil))
    }

    // MARK: Which job's options are already on screen

    private func item(_ id: String) -> TaskItem {
        TaskItem(json: ["task_id": id, "run_id": "run_\(id)", "status": "done", "title": "Weekly report"])!
    }

    /// A question card pinned beside the board shows that job's options: the board names it so its
    /// card leaves out "Other options". Only a job deciding on that very task counts.
    func testPinnedQuestionNamesTheJobDecidingOnIt() {
        let deciding = Job(id: "j1", name: "Weekly report", tasks: [item("a"), item("b")], stage: .needsYou(.decide),
                           decisionTaskID: "b")
        let running = Job(id: "j2", name: "Checkout test", tasks: [item("c")], stage: .running)
        XCTAssertEqual(JobBoard.jobDeciding(taskID: "b", in: [running, deciding]), "j1")
        XCTAssertNil(JobBoard.jobDeciding(taskID: "a", in: [running, deciding]), "an earlier task of the job isn't its question")
        XCTAssertNil(JobBoard.jobDeciding(taskID: "c", in: [running, deciding]))
        XCTAssertNil(JobBoard.jobDeciding(taskID: nil, in: [running, deciding]))
    }
}
