import Foundation
import XCTest
@testable import SpeakeasyCore

/// A failed task says why: the api's `failure` (Speakeasy's own words for Hermes' error) reaches
/// the task row, the task detail, the call's status line and the failure notifications.
final class RunFailureTests: XCTestCase {
    let t0 = Date(timeIntervalSince1970: 1_800_000_000)
    let credits = "Your AI account is out of credits. Add credits there, or switch model in this chat."

    /// One task exactly as the plugin's `GET /voice/work/latest` returns it after Venice's HTTP 402.
    var outOfCreditsJSON: String {
        """
        {"task_id":"call_weather","run_id":"run_0001","status":"failed","stale":false,"updated":1800000000,
         "events":[{"kind":"request","text":"What's the weather in Lisbon?","at":1800000000},
                   {"kind":"result","text":"Didn't finish: \(credits)","at":1800000001}],
         "short_status":null,"detail":null,"result":null,"title":"Lisbon weather",
         "failure":{"kind":"billing","label":"Out of credits","text":"\(credits)"}}
        """
    }

    func failedTask(_ failure: WorkFailure?, request: String = "Order more snacks") -> TaskItem {
        var info = WorkInfo(runID: "run_a", status: "failed", updated: t0,
                            events: [WorkEventItem(kind: "request", text: request, at: t0)])
        info.failure = failure
        return TaskItem(id: "a", info: info)
    }

    func testFailureDecodesFromTheTaskList() throws {
        let object = try JSONSerialization.jsonObject(with: Data(outOfCreditsJSON.utf8))
        let task = try XCTUnwrap(TaskItem(json: object))
        XCTAssertEqual(task.info.failure, WorkFailure(kind: "billing", label: "Out of credits", text: credits))
        XCTAssertNil(task.info.result, "a failure is not an answer")
    }

    func testFailureOnlyCountsForFailedTasksAndNeedsText() {
        let reason: [String: Any] = ["kind": "billing", "label": "Out of credits", "text": credits]
        XCTAssertNil(WorkInfo(json: ["status": "completed", "failure": reason] as [String: Any])?.failure)
        XCTAssertNil(WorkInfo(json: ["status": "failed", "failure": ["kind": "billing", "text": " "]] as [String: Any])?.failure)
        let unknown = WorkInfo(json: ["status": "failed",
                                      "failure": ["kind": "unknown", "text": "Hermes didn't say why."]] as [String: Any])?.failure
        XCTAssertEqual(unknown, WorkFailure(kind: "unknown", label: nil, text: "Hermes didn't say why."))
        // An older plugin sends no failure: the task still decodes, with no reason.
        XCTAssertNil(WorkInfo(json: ["status": "failed", "run_id": "r"] as [String: Any])?.failure)
    }

    func testRowNamesTheReason() {
        let row = taskStatusLine(failedTask(WorkFailure(kind: "billing", label: "Out of credits", text: credits)).info, now: t0)
        XCTAssertEqual(row.0, "Didn't finish · Out of credits")
        XCTAssertEqual(row.1, .error)
        // No known reason (or an older api): the row stays as it was.
        XCTAssertEqual(taskStatusLine(failedTask(WorkFailure(kind: "unknown", text: "Hermes didn't say why.")).info, now: t0).0,
                       "Didn't finish")
        XCTAssertEqual(taskStatusLine(failedTask(nil).info, now: t0).0, "Didn't finish")
    }

    func testDetailExplainsOnlyFailedTasks() {
        XCTAssertEqual(failureDetail(failedTask(WorkFailure(kind: "billing", label: "Out of credits", text: credits)).info), credits)
        XCTAssertNil(failureDetail(failedTask(nil).info))
        XCTAssertNil(failureDetail(WorkInfo(runID: "r", status: "completed")))
        XCTAssertNil(failureDetail(nil))
    }

    func testCallStatusLineNamesTheReason() {
        var s = VoiceState()
        s = reduce(s, .startRequested, now: t0)
        s = reduce(s, .sessionAdmitted(interactionID: "vi_1"), now: t0)
        s = reduce(s, .sessionStarted, now: t0)
        var info = WorkInfo(runID: "run_a", status: "failed", updated: t0)
        info.failure = WorkFailure(kind: "rate_limit", label: "Rate-limited", text: "Wait a minute and try again.")
        s = reduce(s, .work(info), now: t0)
        XCTAssertEqual(present(s).secondary, "Didn't finish · Rate-limited")
        XCTAssertEqual(present(s).tone, .error)
    }

    func testNotificationsSayWhy() {
        let reason = WorkFailure(kind: "billing", label: "Out of credits", text: credits)
        var watch = IdleWorkWatch()
        var running = WorkInfo(runID: "run_a", status: "working")
        watch.callEnded(lastWork: running)
        running.status = "failed"
        running.failure = reason
        XCTAssertEqual(watch.observe(running)?.body, credits)
        // Speakeasy's sentence wins over any text that came back with the failed run.
        var both = IdleWorkWatch()
        var withText = WorkInfo(runID: "run_b", status: "working")
        both.callEnded(lastWork: withText)
        withText.status = "failed"
        withText.failure = reason
        withText.result = WorkResult(spoken: "Partial notes", full: "Partial notes")
        XCTAssertEqual(both.observe(withText)?.body, credits)

        let before = [TaskItem(id: "a", info: WorkInfo(runID: "run_a", status: "working",
                                                        events: [WorkEventItem(kind: "request", text: "Order more snacks", at: t0)]))]
        let paused = PausedTaskNotices.settled(before: before, after: [failedTask(reason)])
        XCTAssertEqual(paused.first?.body, "Couldn't finish “Order more snacks”. \(credits)")
        XCTAssertEqual(PausedTaskNotices.settled(before: before, after: [failedTask(nil)]).first?.body,
                       "Couldn't finish “Order more snacks”.")
    }
}
