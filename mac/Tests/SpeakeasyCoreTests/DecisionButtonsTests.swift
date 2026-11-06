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
}
