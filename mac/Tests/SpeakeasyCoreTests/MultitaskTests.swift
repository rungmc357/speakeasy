import Foundation
import XCTest
@testable import SpeakeasyCore

/// Parallel voice tasks and Pause/Resume.
final class MultitaskTests: XCTestCase {
    let t0 = Date(timeIntervalSince1970: 1_800_000_000)

    func run(_ events: [VoiceEvent], from state: VoiceState = VoiceState()) -> VoiceState {
        var s = state
        var now = t0
        for event in events { s = reduce(s, event, now: now); now += 0.1 }
        return s
    }

    func live() -> VoiceState {
        run([.startRequested, .sessionAdmitted(interactionID: "vi_1"), .sessionStarted])
    }

    func task(_ id: String, _ status: String, request: String, short: String? = "Working on it") -> TaskItem {
        TaskItem(id: id, info: WorkInfo(runID: "run_\(id)", status: status, updated: t0, shortStatus: short,
                                        updatedAt: t0, statusSource: "authored",
                                        events: [WorkEventItem(kind: "request", text: request, at: t0)]))
    }

    // MARK: Tasks

    func testTasksDecodeFromSnapshotAndTasksEvent() {
        let payload = """
        {"interaction":{"interaction_id":"vi_1","finalization":"open","paused":false},"work":null,"approval":null,
         "tasks":[{"task_id":"d1","run_id":"run_1","status":"working","short_status":"Ordering snacks",
                   "events":[{"kind":"request","text":"Order more snacks","at":1800000000}]},
                  {"task_id":"d2","run_id":"run_2","status":"completed","result":{"spoken":"Sunny."}}]}
        """
        let events = parseServerStreamEvent(SSEEvent(name: "snapshot", data: payload, id: "1"))
        let s = run(events, from: live())
        XCTAssertEqual(s.tasks.map(\.id), ["d1", "d2"])
        XCTAssertEqual(s.tasks.first?.info.request, "Order more snacks")
        XCTAssertEqual(s.activeTasks.map(\.id), ["d1"])
        let update = parseServerStreamEvent(SSEEvent(name: "tasks", data: "[]", id: "2"))
        XCTAssertEqual(update, [.tasks([])])
    }

    func testStatusCountsParallelTasks() {
        var s = live()
        s = reduce(s, .work(WorkInfo(runID: "run_b", status: "working", updated: t0, shortStatus: "Checking weather",
                                     updatedAt: t0)), now: t0)
        s = reduce(s, .tasks([task("a", "working", request: "Order snacks"),
                              task("b", "working", request: "Weather tomorrow", short: "Checking weather")]), now: t0)
        XCTAssertEqual(present(s).secondary, "2 tasks")
        s = reduce(s, .tasks([task("a", "completed", request: "Order snacks"),
                              task("b", "working", request: "Weather tomorrow")]), now: t0)
        XCTAssertEqual(present(s).secondary, "Checking weather")
    }

    func testTaskStatusLine() {
        XCTAssertEqual(taskStatusLine(task("a", "working", request: "x", short: "Ordering chips").info, now: t0 + 1).0,
                       "Ordering chips")
        XCTAssertEqual(taskStatusLine(task("a", "failed", request: "x").info, now: t0).0, "Work failed")
        XCTAssertEqual(taskStatusLine(task("a", "waiting_for_approval", request: "x").info, now: t0).0, "Needs your approval")
        XCTAssertEqual(taskStatusLine(task("a", "working", request: "x").info, now: t0 + 200).0, "Status unconfirmed")
    }

    // MARK: Pause / Resume

    func testPauseKeepsConversationAndTasks() {
        var s = run([.inputDelta("Let's plan the snack cart."), .outputDelta("Sweet or salty?")], from: live())
        s = reduce(s, .tasks([task("a", "working", request: "Order snacks")]), now: t0)
        XCTAssertTrue(s.canPause)
        XCTAssertEqual(present(s).pauseLabel, "Pause")
        s = reduce(s, .pauseRequested, now: t0 + 1)
        XCTAssertEqual(s.connection, .ending)
        XCTAssertEqual(present(s).primary, "Pausing…")
        XCTAssertFalse(s.canPause)
        // The session closes: the call is paused, not ended.
        s = reduce(s, .sessionClosed, now: t0 + 2)
        XCTAssertEqual(s.connection, .paused)
        XCTAssertTrue(s.connection.isOpen)
        XCTAssertFalse(s.connection.isInCall)
        XCTAssertEqual(s.exchange.cleaned.assistant, "Sweet or salty?")
        XCTAssertEqual(s.tasks.count, 1)
        let p = present(s)
        XCTAssertEqual(p.primary, "Paused")
        XCTAssertEqual(p.pauseLabel, "Resume")
        XCTAssertTrue(p.pauseEnabled)
        XCTAssertFalse(p.micEnabled)
        // A late api "closed" does not end a paused call.
        s = reduce(s, .serverClosed(.complete, error: nil), now: t0 + 3)
        XCTAssertEqual(s.connection, .paused)
    }

    func testLostOrTimedOutCloseWhilePausingStillPauses() {
        var s = reduce(live(), .pauseRequested, now: t0)
        s = reduce(s, .endTimedOut, now: t0 + 8)
        XCTAssertEqual(s.connection, .paused)
        s = reduce(reduce(live(), .pauseRequested, now: t0), .interaction(InteractionSnapshot(finalization: "confirmed")), now: t0)
        XCTAssertEqual(s.connection, .paused)
    }

    func testPauseFailureStaysLive() {
        var s = reduce(live(), .pauseRequested, now: t0)
        s = reduce(s, .pauseFailed("Couldn't pause: offline"), now: t0 + 1)
        XCTAssertEqual(s.connection, .live)
        XCTAssertFalse(s.pausing)
        XCTAssertEqual(s.lastError, "Couldn't pause: offline")
    }

    func testResumeContinuesTheSameCall() {
        var s = run([.inputDelta("Order more snacks"), .outputDelta("On it.")], from: live())
        s.mic = .muted
        s = run([.tasks([task("a", "working", request: "Order more snacks")]), .pauseRequested, .sessionClosed], from: s)
        s = reduce(s, .resumeRequested, now: t0 + 10)
        XCTAssertEqual(s.connection, .connecting)
        XCTAssertEqual(s.resumeFrom, "vi_1")
        XCTAssertNil(s.interactionID)
        XCTAssertEqual(s.tasks.count, 1, "tasks survive resume")
        XCTAssertEqual(s.mic, .muted, "mic choice survives resume")
        XCTAssertEqual(s.exchange.cleaned.assistant, "On it.", "last exchange stays visible until the user speaks")
        s = run([.sessionAdmitted(interactionID: "vi_2"), .sessionStarted, .inputDelta("Add pretzels")], from: s)
        XCTAssertEqual(s.connection, .live)
        XCTAssertEqual(s.exchange.cleaned.you, "Add pretzels")
        XCTAssertEqual(s.exchange.cleaned.assistant, "")
        // A fresh Start (not Resume) forgets the paused call.
        XCTAssertNil(reduce(s, .startRequested, now: t0 + 20).resumeFrom)
    }

    func testEndWhilePausedEndsWithoutAnotherClose() {
        var s = run([.pauseRequested, .sessionClosed], from: live())
        s = reduce(s, .endRequested, now: t0 + 5)
        XCTAssertEqual(s.connection, .ended(.complete))
        XCTAssertNil(present(s).pauseLabel)
    }

    func testEndWhilePausingEndsTheCall() {
        var s = run([.pauseRequested, .endRequested, .sessionClosed], from: live())
        XCTAssertEqual(s.connection, .ended(.complete))
        s = run([.pauseRequested, .endRequested, .resumeRequested], from: live())
        XCTAssertNotEqual(s.connection, .connecting)
    }

    func testPauseOnlyWhenLive() {
        let connecting = reduce(VoiceState(), .startRequested, now: t0)
        XCTAssertEqual(reduce(connecting, .pauseRequested, now: t0).connection, .connecting)
        XCTAssertFalse(present(connecting).pauseEnabled)
        XCTAssertEqual(reduce(live(), .resumeRequested, now: t0).connection, .live)
    }

    func testResumeRequestBodyNamesThePausedCall() throws {
        let sdp = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\n"
        let body = try JSONSerialization.jsonObject(with: sessionRequestBody(sdp: sdp, resumeFrom: "vi_1")) as? [String: String]
        XCTAssertEqual(body, ["sdp": sdp, "resume_from": "vi_1"])
        let plain = try JSONSerialization.jsonObject(with: sessionRequestBody(sdp: sdp)) as? [String: String]
        XCTAssertEqual(plain, ["sdp": sdp])
    }

    func testTourRequestBodyCarriesShortcutsButNeverOnResume() throws {
        let sdp = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\n"
        let tour = ["call": "⌃⌥Space", "mute": "⌃⌥M"]
        let body = try JSONSerialization.jsonObject(with: sessionRequestBody(sdp: sdp, tour: tour)) as? [String: Any]
        XCTAssertEqual(body?["tour"] as? [String: String], tour)
        let resumed = try JSONSerialization.jsonObject(with: sessionRequestBody(sdp: sdp, resumeFrom: "vi_1", tour: tour))
            as? [String: Any]
        XCTAssertNil(resumed?["tour"], "a resumed call continues the old one; the tour never restarts")
    }

    func testSuggestedListeningShortcutIsValidAndDistinct() {
        XCTAssertEqual(KeyShortcut.suggestedListening.display, "⌃⌥L")
        XCTAssertEqual(KeyShortcut.parse("ctrl+opt+l"), .success(.suggestedListening))
        for other in [KeyShortcut.call, .defaultMute, .defaultPause] {
            XCTAssertNotEqual(KeyShortcut.suggestedListening, other)
        }
    }

    func testRoomRequestBodyRidesOnANewCallOrTheResumeAndReplacesTheTour() throws {
        let sdp = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\n"
        let room = "[10:42] Sam says the deadline is Friday the 14th."
        let tour = ["call": "⌃⌥Space"]
        let body = try JSONSerialization.jsonObject(with: sessionRequestBody(sdp: sdp, tour: tour, room: room)) as? [String: Any]
        XCTAssertEqual(body?["room"] as? String, room)
        XCTAssertEqual(body?["sdp"] as? String, sdp, "the SDP's trailing CRLF must survive")
        XCTAssertNil(body?["tour"], "a call from listening mode isn't the first-call tour")
        let resumed = try JSONSerialization.jsonObject(with: sessionRequestBody(sdp: sdp, resumeFrom: "vi_1", room: room))
            as? [String: String]
        XCTAssertEqual(resumed, ["sdp": sdp, "resume_from": "vi_1", "room": room],
                       "listening mode turned on mid-call: the resume brings what was heard")
        let plainResume = try JSONSerialization.jsonObject(with: sessionRequestBody(sdp: sdp, resumeFrom: "vi_1", tour: tour))
            as? [String: String]
        XCTAssertEqual(plainResume, ["sdp": sdp, "resume_from": "vi_1"], "an ordinary resume is unchanged")
        let empty = try JSONSerialization.jsonObject(with: sessionRequestBody(sdp: sdp, room: "")) as? [String: String]
        XCTAssertEqual(empty, ["sdp": sdp], "nothing heard means no room field (the exact body older servers accept)")
    }

    func testStatusSaysWhetherTheServerTakesRoomText() throws {
        let newer = try JSONDecoder().decode(ServerStatus.self, from: Data(#"{"version":"0.2.51","room_listening":true,"room_on_resume":true}"#.utf8))
        XCTAssertEqual(newer.roomListening, true)
        XCTAssertEqual(newer.roomOnResume, true)
        let firstCut = try JSONDecoder().decode(ServerStatus.self, from: Data(#"{"room_listening":true}"#.utf8))
        XCTAssertNil(firstCut.roomOnResume, "a plugin that takes room text only on new calls can't do listening mode during a call")
        let older = try JSONDecoder().decode(ServerStatus.self, from: Data(#"{"version":"0.2.50"}"#.utf8))
        XCTAssertNil(older.roomListening, "older plugins reject unknown session fields: never send room to them")
    }

    func testPauseShortcutDefaultIsValidAndDistinct() {
        XCTAssertEqual(KeyShortcut.defaultPause.display, "⌃⌥P")
        XCTAssertEqual(KeyShortcut.parse("ctrl+opt+p"), .success(.defaultPause))
        XCTAssertNotEqual(KeyShortcut.defaultPause, KeyShortcut.defaultMute)
        XCTAssertNotEqual(KeyShortcut.defaultPause, KeyShortcut.call)
    }

    // MARK: Heads-up while paused

    func pausedTask(_ id: String, _ status: String, request: String = "Order more snacks",
                    spoken: String? = nil) -> TaskItem {
        TaskItem(id: id, info: WorkInfo(runID: "run_" + id, status: status,
                                        events: [WorkEventItem(kind: "request", text: request, at: t0)],
                                        result: spoken.map { WorkResult(spoken: $0, full: $0) }))
    }

    func testPausedHeadsUpOnlyForTasksThatJustSettled() {
        let before = [pausedTask("a", "working"), pausedTask("b", "working", request: "Text Dana"),
                      pausedTask("c", "completed")]
        let after = [pausedTask("a", "completed", spoken: "Snacks ordered, arriving Tuesday."),
                     pausedTask("b", "waiting_for_approval", request: "Text Dana"),
                     pausedTask("c", "completed"), pausedTask("d", "completed")]
        let notices = PausedTaskNotices.settled(before: before, after: after)
        XCTAssertEqual(notices.map(\.kind), [.completed, .approval])
        XCTAssertEqual(notices[0].body, "Snacks ordered, arriving Tuesday.")
        XCTAssertEqual(notices[0].title, "Hermes · call paused")
        XCTAssertEqual(notices[1].body, "Needs your approval: “Text Dana”")
    }

    func testPausedHeadsUpFailureAndNoChange() {
        let before = [pausedTask("a", "working")]
        XCTAssertTrue(PausedTaskNotices.settled(before: before, after: before).isEmpty)
        let failed = PausedTaskNotices.settled(before: before, after: [pausedTask("a", "failed")])
        XCTAssertEqual(failed.first?.kind, .failed)
        XCTAssertEqual(failed.first?.body, "Couldn't finish “Order more snacks”.")
    }

    // MARK: Titles and clearing

    func testTaskUsesTitleOverRequestWhenNamed() {
        var info = WorkInfo(runID: "r1", status: "working",
                            events: [WorkEventItem(kind: "request", text: "Forecast for Saturday?", at: nil)])
        XCTAssertEqual(TaskItem(id: "d1", info: info).name, "Forecast for Saturday?")
        info.title = "Weekend weather"
        XCTAssertEqual(TaskItem(id: "d1", info: info).name, "Weekend weather")
        let parsed = TaskItem(json: ["task_id": "d1", "run_id": "r1", "status": "working", "title": "Weekend weather"] as [String: Any])
        XCTAssertEqual(parsed?.name, "Weekend weather")
    }

    func testDismissHidesOnlyFinishedTasksAndStaysHidden() {
        let done = TaskItem(id: "d1", info: WorkInfo(runID: "r1", status: "completed"))
        let running = TaskItem(id: "d2", info: WorkInfo(runID: "r2", status: "working"))
        var s = reduce(VoiceState(), .tasks([done, running]), now: Date())
        s = reduce(s, .tasksDismissed(["r1", "r2"]), now: Date())
        XCTAssertEqual(s.tasks.map(\.id), ["d2"], "a running task is never cleared")
        // A later snapshot from the api that still carries it does not bring it back.
        s = reduce(s, .tasks([done, running]), now: Date())
        XCTAssertEqual(s.tasks.map(\.id), ["d2"])
    }
}
