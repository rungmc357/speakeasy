import XCTest
@testable import SpeakeasyCore

final class JobBoardTests: XCTestCase {
    let now = Date()

    func task(_ id: String, _ status: String = "completed", job: [String: Any]? = nil, title: String? = nil,
              age: TimeInterval = 10, views: [[String: Any]]? = nil, plan: [String]? = nil, milestones: Int = 0) -> TaskItem {
        var json: [String: Any] = ["task_id": id, "run_id": "run_\(id)", "status": status,
                                   "updated_at": now.timeIntervalSince1970 - age]
        if let job { json["job"] = job }
        if let title { json["title"] = title }
        if let views { json["result"] = ["views": views] }
        if let plan { json["plan"] = plan }
        json["events"] = (0..<milestones).map { ["kind": "milestone", "text": "Step \($0)"] }
        return TaskItem(json: json)!
    }

    // MARK: Decoding

    func testJobDecodesAndIsOptional() {
        let t = task("a", job: ["id": "sess_1", "name": "Release v2.4"])
        XCTAssertEqual(t.info.job, JobRef(id: "sess_1", name: "Release v2.4"))
        XCTAssertNil(task("b").info.job)
        XCTAssertNil(task("c", job: ["name": "No id"]).info.job, "a job without an id is ignored")
        XCTAssertEqual(task("d", job: ["id": "x", "name": "  "]).info.job?.name, "Job", "never empty")
    }

    func testDecisionDecodesObjectOptionsAndStrings() {
        let card = ViewCard(kind: "question", data: [
            "question": "When should the router stop sending work?", "context": "Two machines share the work.",
            "why": "Keeps the swap protection.", "affects": "1 file · all local jobs", "approvable": true, "recommended": 0,
            "options": [
                ["text": "1 error, 30s bench", "detail": "Keeps the swap protection.",
                 "tags": [["text": "Low risk", "tone": "good"], "Plain tag"], "risk": "low", "reversible": true, "image": 2],
                ["text": "3 errors, 30s bench", "risk": "medium", "reversible": false],
                "Leave it", ["detail": "no text, dropped"], ""]])
        let d = Decision(card: card)!
        XCTAssertEqual(d.options.map(\.text), ["1 error, 30s bench", "3 errors, 30s bench", "Leave it"])
        XCTAssertEqual(d.options[0].tags, [DecisionTag(text: "Low risk", tone: "good"), DecisionTag(text: "Plain tag")])
        XCTAssertEqual(d.options[0].risk, "low")
        XCTAssertEqual(d.options[0].reversible, true)
        XCTAssertEqual(d.options[0].image, 2)
        XCTAssertEqual(d.options[1].reversible, false)
        XCTAssertNil(d.options[2].reversible)
        XCTAssertEqual(d.context, "Two machines share the work.")
        XCTAssertEqual(d.why, "Keeps the swap protection.")
        XCTAssertEqual(d.affects, "1 file · all local jobs")
        XCTAssertTrue(d.approvable)
        XCTAssertEqual(d.recommendedOption?.text, "1 error, 30s bench")
    }

    func testOldStringQuestionStillWorks() {
        let d = Decision(card: ViewCard(kind: "question", data: ["question": "Do it?", "options": ["Yes", "No"], "recommended": 1]))!
        XCTAssertEqual(d.options.map(\.text), ["Yes", "No"])
        XCTAssertEqual(d.recommended, 1)
        XCTAssertFalse(d.approvable)
        XCTAssertNil(d.context)
    }

    func testApprovableNeedsARecommendation() {
        let d = Decision(card: ViewCard(kind: "question", data: ["question": "Q", "options": ["A", "B"], "approvable": true]))!
        XCTAssertFalse(d.approvable)
        let outOfRange = Decision(card: ViewCard(kind: "question", data: ["question": "Q", "options": ["A"], "recommended": 4, "approvable": true]))!
        XCTAssertNil(outOfRange.recommended)
        XCTAssertFalse(outOfRange.approvable)
    }

    func testNewVisualKindsDecode() {
        let views = ViewCard.list(json: [
            ["kind": "lanes", "title": "Last night", "lanes": [["label": "Backup", "segments": [["from": 0.1, "to": 0.4, "tone": "bad"]]]]],
            ["kind": "before_after", "before": ["label": "Before", "text": "4 rows"], "after": ["label": "After", "text": "1 row"]],
            ["kind": "flow", "nodes": [["label": "Repo"], ["label": "Plugin"]]],
            ["kind": "devices", "items": [["name": "Laptop", "value": "1.4.35", "history": [1, 2, 3]]]],
        ])
        XCTAssertEqual(views.map(\.kind), ["lanes", "before_after", "flow", "devices"])
        XCTAssertEqual(views[3]["items"].array.first?["history"].doubles, [1, 2, 3])
    }

    // MARK: Grouping

    func testTasksWithoutAJobAreOneJobEach() {
        let jobs = JobBoard.jobs([task("a", title: "Weather"), task("b", title: "Lights")])
        XCTAssertEqual(jobs.map(\.name), ["Lights", "Weather"], "newest job first")
        XCTAssertEqual(jobs.map { $0.tasks.count }, [1, 1])
    }

    func testTasksSharingAJobGroupTogether() {
        let rel = ["id": "s1", "name": "Release v2.4"]
        let tasks = [task("a", job: rel, title: "Build"), task("b", title: "Weather"), task("c", "working", job: rel, title: "Ship")]
        let jobs = JobBoard.jobs(tasks)
        XCTAssertEqual(jobs.map(\.name), ["Release v2.4", "Weather"])
        XCTAssertEqual(jobs[0].tasks.map(\.id), ["a", "c"])
        XCTAssertEqual(jobs[0].newest.id, "c")
        XCTAssertEqual(jobs[0].stage, .running)
        XCTAssertEqual(jobs[1].stage, .done)
    }

    func testStages() {
        let q: [[String: Any]] = [["kind": "question", "question": "Ship it?", "options": ["Yes", "No"], "recommended": 0]]
        let tasks = [task("ask", views: q), task("appr", "waiting_for_approval"), task("fail", "failed"),
                     task("run", "working"), task("done")]
        var jobs = JobBoard.jobs(tasks)
        func stage(_ id: String) -> Job.Stage? { jobs.first { $0.tasks.contains { $0.id == id } }?.stage }
        XCTAssertEqual(stage("ask"), .needsYou(.decide))
        XCTAssertEqual(stage("appr"), .needsYou(.approve))
        XCTAssertEqual(stage("fail"), .failed)
        XCTAssertEqual(stage("run"), .running)
        XCTAssertEqual(stage("done"), .done)
        XCTAssertEqual(jobs.first { $0.id == "task:ask" }?.decisionTaskID, "ask")
        // Answered or dismissed: no longer waiting.
        jobs = JobBoard.jobs(tasks, answered: ["ask"])
        XCTAssertEqual(stage("ask"), .done)
        jobs = JobBoard.jobs(tasks, dismissed: ["ask"])
        XCTAssertEqual(stage("ask"), .done)
        // Pictures waiting to be looked at: nothing to act on, so not Needs you.
        jobs = JobBoard.jobs(tasks, reviewing: ["done"])
        XCTAssertEqual(stage("done"), .done)
        XCTAssertEqual(jobs.first { $0.id == "task:done" }?.lookTaskID, "done")
        // An old question falls off.
        jobs = JobBoard.jobs([task("ask", age: 2 * 86400, views: q)])
        XCTAssertEqual(jobs.first?.stage, .done)
    }

    func testSetAsideLeavesNeedsYouForDoneToday() {
        let q: [[String: Any]] = [["kind": "question", "question": "Which lamp?", "options": ["Living room", "All of them"]]]
        var asked = TaskItem(json: ["task_id": "ask", "run_id": "run_ask", "status": "completed", "set_aside": true,
                                    "updated_at": now.timeIntervalSince1970 - 10, "result": ["views": q]])!
        XCTAssertTrue(asked.info.setAside, "the api's set_aside decodes")
        XCTAssertFalse(task("other").info.setAside, "absent means not set aside")
        let tasks = [asked, task("appr", "waiting_for_approval"), task("run", "working")]
        var jobs = JobBoard.jobs(tasks)
        func job(_ id: String) -> Job? { jobs.first { $0.tasks.contains { $0.id == id } } }
        XCTAssertEqual(job("ask")?.stage, .done)
        XCTAssertEqual(job("ask")?.setAside, true)
        XCTAssertNil(job("ask")?.answerTask, "set aside is not a new answer to read")
        XCTAssertEqual(OrbState.of(jobs: jobs, userTalking: false), .needsYou(1), "only the approval still needs you")
        // Set aside here before the api confirms (an approval this time): nothing needs you.
        jobs = JobBoard.jobs(tasks, setAside: ["appr"])
        XCTAssertEqual(job("appr")?.stage, .done)
        XCTAssertEqual(job("appr")?.setAside, true)
        XCTAssertEqual(OrbState.of(jobs: jobs, userTalking: true), .talking, "amber only while something needs you")
        let sections = JobBoard.sections(jobs, now: now)
        XCTAssertTrue(sections.needsYou.isEmpty)
        XCTAssertEqual(Set(sections.doneToday.map(\.id)), ["task:ask", "task:appr"])
        // A later task in the same job that asks again needs you again.
        asked.info.job = JobRef(id: "j", name: "Lamp")
        let again = task("ask2", job: ["id": "j", "name": "Lamp"], age: 5, views: q)
        jobs = JobBoard.jobs([asked, again])
        XCTAssertEqual(jobs.first?.stage, .needsYou(.decide))
        XCTAssertEqual(jobs.first?.setAside, false)
    }

    func testSectionsOrderDecisionsFirst() {
        let q: [[String: Any]] = [["kind": "question", "question": "Ship it?", "options": ["Yes", "No"]]]
        let tasks = [task("ask", views: q), task("appr", "waiting_for_approval"), task("run", "working"), task("done"),
                     task("old", age: 3 * 86400)]
        let s = JobBoard.sections(JobBoard.jobs(tasks), now: now)
        XCTAssertEqual(s.needsYou.map(\.newest.id), ["ask", "appr"])
        XCTAssertEqual(s.running.map(\.newest.id), ["run"])
        XCTAssertEqual(s.doneToday.map(\.newest.id), ["done"], "yesterday's work isn't 'done today'")
        XCTAssertEqual(s.all.count, 4)
    }

    func testProgressFromPlan() {
        XCTAssertNil(JobBoard.progress(task("a", "working").info))
        let p = JobBoard.progress(task("a", "working", plan: ["A", "B", "C", "D", "E"], milestones: 2).info)!
        XCTAssertEqual(p.done, 2); XCTAssertEqual(p.total, 5)
        XCTAssertEqual(JobBoard.progress(task("a", "working", plan: ["A", "B"], milestones: 9).info)!.done, 1, "never full while running")
        XCTAssertEqual(JobBoard.progress(task("a", plan: ["A", "B"]).info)!.done, 2)
    }

    // MARK: Orb

    func testOrbStates() {
        let q: [[String: Any]] = [["kind": "question", "question": "Ship it?", "options": ["Yes", "No"]]]
        XCTAssertEqual(OrbState.of(jobs: [], userTalking: false), .idle)
        XCTAssertEqual(OrbState.of(jobs: [], userTalking: true), .talking)
        XCTAssertEqual(OrbState.of(jobs: JobBoard.jobs([task("r", "working")]), userTalking: false), .working)
        let needs = JobBoard.jobs([task("r", "working"), task("q", views: q), task("a", "waiting_for_approval")])
        XCTAssertEqual(OrbState.of(jobs: needs, userTalking: true), .needsYou(2))
        XCTAssertEqual(OrbState.of(jobs: needs, userTalking: true).badge, 2)
        XCTAssertEqual(OrbState.of(jobs: [], approvalPending: true, userTalking: false), .needsYou(1))
        XCTAssertEqual(OrbState.of(jobs: JobBoard.jobs([task("f", "failed"), task("q", views: q)]), userTalking: false), .failed(needs: 1))
        XCTAssertNil(OrbState.failed(needs: 0).badge)
    }

    // MARK: Answers

    func answer(_ id: String, result: [String: Any], age: TimeInterval = 10) -> TaskItem {
        TaskItem(json: ["task_id": id, "run_id": "run_\(id)", "status": "completed", "title": id,
                        "updated_at": now.timeIntervalSince1970 - age, "result": result])!
    }

    func testUnreadAnswersGetTheirOwnSectionUntilSeen() {
        let q: [[String: Any]] = [["kind": "question", "question": "Ship it?", "options": ["Yes", "No"]]]
        let wx = answer("wx", result: ["spoken": "Sunny.", "views": [["kind": "weather", "temp": 22, "condition": "Sunny"]]])
        let tasks = [task("ask", views: q), wx, task("run", "working"), answer("old", result: ["spoken": "Hi."], age: 3 * 86400)]
        let jobs = JobBoard.jobs(tasks)
        var s = JobBoard.sections(jobs, now: now)
        XCTAssertEqual(s.needsYou.map(\.newest.id), ["ask"])
        XCTAssertEqual(s.newAnswers.map(\.newest.id), ["wx"])
        XCTAssertEqual(s.all.map(\.newest.id), ["ask", "wx", "run"], "answers sit between Needs you and Running; yesterday's drop off")
        XCTAssertTrue(s.doneToday.isEmpty)
        s = JobBoard.sections(jobs, seen: ["run_wx"], now: now)
        XCTAssertTrue(s.newAnswers.isEmpty)
        XCTAssertEqual(s.doneToday.map(\.newest.id), ["wx"], "a read answer is a Done today chip")
        // A question is a decision, not an answer.
        XCTAssertNil(JobBoard.jobs([answer("q", result: ["spoken": "Ship?", "views": q])], answered: ["q"]).first?.answerTask)
    }

    func testAHomeCommandTheServerMarkedSeenIsNeverANewAnswer() {
        var lights = answer("lights", result: ["spoken": "Kitchen lights are off."])
        XCTAssertFalse(lights.info.seen)
        lights = TaskItem(json: ["task_id": "lights", "run_id": "run_lights", "status": "completed", "title": "Lights off",
                                 "updated_at": now.timeIntervalSince1970 - 10, "result": ["spoken": "Done."], "seen": true])!
        XCTAssertTrue(lights.info.seen)
        let wx = answer("wx", result: ["spoken": "Sunny."])
        let s = JobBoard.sections(JobBoard.jobs([lights, wx]), now: now)
        XCTAssertEqual(s.newAnswers.map(\.newest.id), ["wx"], "the read-out still waits to be read")
        XCTAssertEqual(s.doneToday.map(\.newest.id), ["lights"], "the home command goes straight to Done today, still openable")
    }

    func reviewed(_ id: String, result: [String: Any], status: String = "completed") -> TaskItem {
        var r = result
        r["cards"] = [["kind": "image", "name": "page.png"]]
        return TaskItem(json: ["task_id": id, "run_id": "run_\(id)", "status": status, "title": id,
                               "updated_at": now.timeIntervalSince1970 - 10, "result": r,
                               "review": ["images": [1], "settled_at": now.timeIntervalSince1970 - 10]])!
    }

    func testIncidentalPicturesDontMakeAnAnswerNeedYou() {
        let report = String(repeating: "Saturday: a jazz brunch at eleven and a night market from seven. ", count: 4)
        let cases: [(TaskItem, Bool)] = [
            // A finished report brought a picture along: an answer.
            (reviewed("shape", result: ["spoken": "Six events.", "shape": ["type": "research", "headline": "Six events",
                                         "sections": [["title": "Saturday", "points": ["Brunch"]]]]]), true),
            (reviewed("long", result: ["full": report]), true),
            (reviewed("views", result: ["spoken": "Sunny.", "views": [["kind": "weather", "temp": 22]]]), true),
            // The picture is the deliverable: still a review.
            (reviewed("design", result: ["spoken": "The landing page is ready.", "full": "Three takes on the bakery landing page."]), false),
            (reviewed("bare", result: [:]), false),
            // A question with a picture still needs you to decide.
            (reviewed("ask", result: ["full": report, "views": [["kind": "question", "question": "Ship it?", "options": ["Yes", "No"]]]]), false),
        ]
        for (t, incidental) in cases {
            XCTAssertEqual(t.info.reviewImages, [1])
            XCTAssertEqual(t.info.reviewIsIncidental, incidental, t.id)
            let job = JobBoard.jobs([t], reviewing: [t.id], now: now).first!
            // Only the question needs you; pictures alone are new (Look), never Needs you.
            XCTAssertEqual(job.stage.needsYou, t.id == "ask", t.id)
            XCTAssertEqual(job.answerTask != nil, incidental, t.id)
            if t.id != "ask" {
                XCTAssertEqual(JobBoard.sections([job], now: now).newAnswers.map(\.id), [job.id], t.id)
                XCTAssertEqual(job.lookTask != nil, !incidental, t.id)
            }
        }
        // No pictures waiting: nothing incidental (the plain answer path applies).
        var none = reviewed("none", result: ["full": report])
        XCTAssertTrue(none.info.reviewIsIncidental)
        none.info.reviewImages = []
        XCTAssertFalse(none.info.reviewIsIncidental, "no pictures waiting, nothing incidental")
    }

    func testAnswerHeadlineAndGlance() {
        let shaped = answer("a", result: ["spoken": "One. Two.", "label": "Checked", "shape": ["type": "answer", "headline": "26 of 28 passed"]])
        XCTAssertEqual(JobAnswer.headline(shaped.info), "26 of 28 passed")
        XCTAssertEqual(JobAnswer.headline(answer("b", result: ["spoken": "One. Two.", "label": "Saturday is drier"]).info), "Saturday is drier")
        XCTAssertEqual(JobAnswer.headline(answer("c", result: ["spoken": "Sunny all day. Rain later."]).info), "Sunny all day.")
        let views: [[String: Any]] = [["kind": "stats", "items": [["label": "Runs", "value": "28"]]],
                                      ["kind": "chart", "series": [["name": "Runs", "values": [4, 3, 5]]]],
                                      ["kind": "list", "items": [["text": "x"]]]]
        XCTAssertEqual(JobAnswer.glanceCard(answer("d", result: ["spoken": "x", "views": views]).info)?.kind, "chart")
        XCTAssertNil(JobAnswer.glanceCard(answer("e", result: ["spoken": "x", "views": [views[2]]]).info), "no glance without a drawable value")
    }

    func testAnswerDotYieldsToNeedsYou() {
        XCTAssertTrue(OrbState.idle.showsAnswerDot(unread: 1))
        XCTAssertTrue(OrbState.working.showsAnswerDot(unread: 2))
        XCTAssertFalse(OrbState.idle.showsAnswerDot(unread: 0))
        XCTAssertFalse(OrbState.needsYou(1).showsAnswerDot(unread: 3))
        XCTAssertFalse(OrbState.failed(needs: 0).showsAnswerDot(unread: 3))
    }

    // MARK: Needs you only when there is something to act on

    func testPicturesToLookAtAreNewNotNeedsYouAndClearOnceSeen() {
        let pics = reviewed("pics", result: ["spoken": "Three takes on the landing page."])
        let job = JobBoard.jobs([pics], reviewing: ["pics"], now: now).first!
        XCTAssertEqual(job.stage, .done)
        XCTAssertNil(job.answerTask, "pictures that are the deliverable are not an answer")
        XCTAssertEqual(job.newTask?.id, "pics")
        var s = JobBoard.sections([job], now: now)
        XCTAssertTrue(s.needsYou.isEmpty)
        XCTAssertEqual(s.newAnswers.map(\.id), [job.id])
        // Opened (or dismissed): it clears into Done today.
        s = JobBoard.sections([job], seen: [JobBoard.seenKey(pics)], now: now)
        XCTAssertTrue(s.newAnswers.isEmpty)
        XCTAssertEqual(s.doneToday.map(\.id), [job.id])
        // Pictures from an earlier day still show as new until looked at, then drop off.
        let later = Calendar.current.date(byAdding: .day, value: 3, to: now)!
        XCTAssertEqual(JobBoard.sections([job], now: later).newAnswers.map(\.id), [job.id])
        XCTAssertTrue(JobBoard.sections([job], seen: [JobBoard.seenKey(pics)], now: later).all.isEmpty)
        XCTAssertEqual(JobAnswer.lookLine(pics.info), "Pictures to look at")
    }

    func testOnlyActionableThingsNeedYou() {
        let q: [[String: Any]] = [["kind": "question", "question": "Ship it?", "options": ["Yes", "No"]]]
        let draft = TaskItem(json: ["task_id": "draft", "run_id": "run_draft", "status": "completed",
                                    "updated_at": now.timeIntervalSince1970 - 10,
                                    "email_drafts": [["draft_id": "d1", "sha256": "abc", "from": "me@example.com",
                                                      "to": ["a@example.com"], "subject": "Hi", "body": "Hello",
                                                      "status": "pending"]]])!
        let cases: [(TaskItem, Set<String>, Job.Stage)] = [
            (task("ask", views: q), [], .needsYou(.decide)),
            (task("appr", "waiting_for_approval"), [], .needsYou(.approve)),
            (reviewed("pics", result: ["spoken": "Here they are."]), ["pics"], .done),
            (reviewed("picsAsk", result: ["views": q]), ["picsAsk"], .needsYou(.decide)),
            (task("plain"), [], .done),
            (task("run", "working", plan: ["One", "Two"]), [], .running),
        ]
        for (t, reviewing, want) in cases {
            XCTAssertEqual(JobBoard.jobs([t], reviewing: reviewing, now: now).first?.stage, want, t.id)
        }
        XCTAssertFalse(draft.info.pendingDrafts.isEmpty)
        XCTAssertEqual(JobBoard.jobs([draft], now: now).first?.stage, .needsYou(.review), "a draft to send or deny is an action")
    }

    // MARK: One source of truth for polled task rows

    func testRefreshNeverWritesBackAStaleCopy() {
        // The Live Activity's after-call poll started from the call's list: the job was on its last step.
        let running = task("t", "working", job: ["id": "j", "name": "Where'd we leave off"], age: 300,
                           plan: ["Check", "Compare", "Fix", "Your review"], milestones: 3)
        // Meanwhile the board's own poll has it finished, with pictures, plus a follow-up typed later.
        var done = task("t", "completed", job: ["id": "j", "name": "Where'd we leave off"], age: 5)
        done.info.reviewImages = [1]
        let followUp = task("typed", "completed", job: ["id": "j", "name": "Where'd we leave off"], age: 2)
        let current = [done, followUp]
        // A row fetched earlier (older than the list) never reopens the finished task.
        let merged = TaskRefresh.merge(current, fresh: ["t": running.info])
        XCTAssertEqual(merged, current, "a stale running row must not replace a finished one")
        XCTAssertEqual(JobBoard.jobs(merged, now: now).first?.stage, .done)
        // A newer row does apply, onto the current list (nothing it didn't fetch changes).
        let fresher = task("t", "completed", job: ["id": "j", "name": "Where'd we leave off"], age: 1)
        let applied = TaskRefresh.merge(current, fresh: ["t": fresher.info, "gone": fresher.info])
        XCTAssertEqual(applied.map(\.id), ["t", "typed"])
        XCTAssertEqual(applied[0].info, fresher.info)
        // A running task moves on with a newer running row.
        let step4 = task("t", "working", age: 1, plan: ["A", "B"], milestones: 1)
        XCTAssertEqual(TaskRefresh.merge([task("t", "working", age: 30)], fresh: ["t": step4.info])[0].info, step4.info)
        // Even a newer-stamped row can't reopen a settled task.
        let reopened = task("t", "working", age: 0)
        XCTAssertEqual(TaskRefresh.merge(current, fresh: ["t": reopened.info]), current)
    }
}
