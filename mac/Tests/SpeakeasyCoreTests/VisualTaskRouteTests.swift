import XCTest
@testable import SpeakeasyCore

/// Opening a task lands on its session page (live line + So far), the old job page only as a fallback.
final class VisualTaskRouteTests: XCTestCase {
    private func task(_ id: String, status: String = "running", session: String? = nil, job: String? = nil,
                      drafts: Bool = false, review: Bool = false) -> TaskItem {
        var json: [String: Any] = ["task_id": id, "run_id": "run_\(id)", "status": status, "title": "Find why the checkout test fails"]
        if let session { json["session_id"] = session }
        if let job { json["job"] = ["id": job, "name": "Checkout test"] }
        if drafts {
            json["email_drafts"] = [["draft_id": "d1", "sha256": "abc", "from": "me@example.com", "to": ["a@example.com"],
                                     "subject": "Lunch Friday", "body": "Friday works.", "status": "pending"]]
        }
        if review {
            json["result"] = ["full": "Done", "cards": [["kind": "image", "name": "a.png"], ["kind": "image", "name": "b.png"]]]
            json["review"] = ["images": [1, 2], "settled_at": 100]
        }
        return TaskItem(json: json)!
    }

    func testOpensTheSessionPage() {
        let jobs = JobBoard.jobs([task("a", session: "s1")])
        XCTAssertEqual(VisualTaskRoute.sessionID(for: jobs[0], historySupported: true), "s1")
    }

    func testOldServerKeepsTheJobPage() {
        let jobs = JobBoard.jobs([task("a", session: "s1")])
        XCTAssertNil(VisualTaskRoute.sessionID(for: jobs[0], historySupported: false))
    }

    func testNoSessionYetKeepsTheJobPage() {
        let jobs = JobBoard.jobs([task("a")])
        XCTAssertNil(VisualTaskRoute.sessionID(for: jobs[0], historySupported: true))
    }

    func testFollowUpInANewerSessionWins() {
        let jobs = JobBoard.jobs([task("a", status: "completed", session: "s1", job: "j"), task("b", session: "s2", job: "j"),
                                  task("c", status: "queued", job: "j")])
        XCTAssertEqual(jobs.count, 1)
        XCTAssertEqual(VisualTaskRoute.sessionID(for: jobs[0], historySupported: true), "s2",
                       "the newest task with a session; one with none yet doesn't hide it")
    }

    func testDraftsAndPicturesOpenTheSessionPage() {
        let drafted = JobBoard.jobs([task("a", session: "s1", drafts: true)])
        XCTAssertEqual(VisualTaskRoute.sessionID(for: drafted[0], historySupported: true), "s1", "drafts are sent from the session page")
        XCTAssertNil(VisualTaskRoute.sessionID(for: drafted[0], historySupported: false), "an older server keeps the panel page")
        let look = JobBoard.jobs([task("b", status: "completed", session: "s1", review: true)], reviewing: ["b"])
        XCTAssertNotNil(look[0].lookTask)
        XCTAssertEqual(VisualTaskRoute.sessionID(for: look[0], historySupported: true), "s1", "pictures are looked at on the session page")
    }

    func testOneTaskOpensItsOwnSession() {
        let tasks = [task("a", status: "completed", session: "s1", job: "j"), task("b", session: "s2", job: "j")]
        let jobs = JobBoard.jobs(tasks)
        XCTAssertEqual(VisualTaskRoute.sessionID(forTask: "a", in: jobs, tasks: tasks, historySupported: true), "s1")
        XCTAssertEqual(VisualTaskRoute.sessionID(forTask: "b", in: jobs, tasks: tasks, historySupported: true), "s2")
        XCTAssertNil(VisualTaskRoute.sessionID(forTask: "a", in: jobs, tasks: tasks, historySupported: false))
        XCTAssertNil(VisualTaskRoute.sessionID(forTask: "zzz", in: jobs, tasks: tasks, historySupported: true))
        let drafted = [task("d", session: "s3", drafts: true)]
        XCTAssertEqual(VisualTaskRoute.sessionID(forTask: "d", in: JobBoard.jobs(drafted), tasks: drafted, historySupported: true), "s3")
        XCTAssertEqual(VisualTaskRoute.sessionID(forTask: "d", in: [], tasks: drafted, historySupported: true), "s3", "also without a job")
    }

    func testSessionPageShowsItsOwnTasksDraftsAndPictures() {
        let mine = task("a", session: "s1", drafts: true)
        let linked = task("b", status: "completed", session: "s_linked", review: true)
        let other = task("c", status: "completed", session: "s2", review: true)
        let tasks = [mine, linked, other]
        let ids = SessionReviewCards.taskIDs(sessionID: "s1", runIDs: ["run_b"], tasks: tasks)
        XCTAssertEqual(ids, ["a", "b"], "its own session's tasks plus the linked tasks the page lists")
        var state = VoiceState()
        state.tasks = tasks
        XCTAssertEqual(SessionReviewCards.drafts(state.pendingDrafts, taskIDs: ids, tasks: tasks).map(\.draftID), ["d1"])
        XCTAssertEqual(SessionReviewCards.reviews(state.pendingReviews, taskIDs: ids).map(\.taskID), ["b"])
        XCTAssertTrue(SessionReviewCards.drafts(state.pendingDrafts, taskIDs: ["c"], tasks: tasks).isEmpty)
        XCTAssertTrue(SessionReviewCards.reviews(state.pendingReviews, taskIDs: []).isEmpty)
    }

    // MARK: The card's live lines

    func testLiveLinePrefersNowLineThenFallsBack() {
        let full = WorkInfo(json: ["status": "running", "short_status": "Handoff guess", "now_line": "Editing two files",
                                   "so_far": "Read the code, edited 2 files"])!
        XCTAssertEqual(TaskLiveLine.now(full), "Editing two files")
        XCTAssertEqual(TaskLiveLine.soFar(full), "Read the code, edited 2 files")
        let old = WorkInfo(json: ["status": "running", "short_status": "Checking the logs"])!
        XCTAssertEqual(TaskLiveLine.now(old), "Checking the logs", "older plugins: the short status")
        XCTAssertNil(TaskLiveLine.soFar(old))
        let same = WorkInfo(json: ["status": "running", "now_line": "Ran the tests", "so_far": "Ran the tests"])!
        XCTAssertNil(TaskLiveLine.soFar(same), "no recap that repeats the live line")
        XCTAssertNil(TaskLiveLine.now(WorkInfo(json: ["status": "running", "short_status": "  "])!))
    }
}
