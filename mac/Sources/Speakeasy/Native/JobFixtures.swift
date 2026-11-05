import Foundation
import SpeakeasyCore
import SpeakeasyClient

/// Job-view fixtures: a board with several jobs cooking, and three jobs at the decision stage and
/// after the pick. Generic data only (this repo is public).
extension PreviewFixtures {
    static let jobNames = ["jobs-board", "job-release", "job-release-after", "job-nightly", "job-nightly-after",
                           "job-rollout", "job-rollout-after",
                           "orb-idle", "orb-talking", "orb-working", "orb-needs", "orb-failed", "orb-answers",
                           "jobs-answers", "answer-weather", "answer-nightly", "answer-laptops",
                           "declutter-board", "declutter-weather", "declutter-events", "declutter-three", "declutter-many",
                           "events-board", "answer-events", "answer-matchup", "jobs-set-aside", "job-set-aside",
                           "job-receipts", "job-not-read", "jobs-fenced", "job-fenced"]

    /// Answers the fixtures treat as already read (run ids), so only the new ones show as new.
    static let readAnswers = ["run_weather", "run_notes"]

    /// Task ids whose question the fixtures answer in the "-after" states.
    static let jobQuestionTasks = ["rel_q", "night_q", "roll_q", "router_q"]

    /// How a fixture is shown: which job opens, what's already answered, folded into the orb or not,
    /// and the mic level (the user talking).
    struct JobSetup {
        var open: String?
        var answers: [String: String] = [:]
        var orb = false
        var level: Double = 0
        var seen: [String] = PreviewFixtures.readAnswers
        /// Task ids whose question the user set aside ("Not needed").
        var setAside: [String] = []
    }

    static func jobSetup(_ name: String) -> JobSetup? {
        switch name {
        case "jobs-board": return JobSetup()
        case "job-release": return JobSetup(open: "job:rel")
        case "job-release-after": return JobSetup(open: "job:rel", answers: ["rel_q": "Ship the plugin now, the app after you try it"])
        case "job-nightly": return JobSetup(open: "job:night")
        case "job-nightly-after": return JobSetup(open: "job:night", answers: ["night_q": "Keep both fixes"])
        case "job-rollout": return JobSetup(open: "job:roll")
        case "job-rollout-after": return JobSetup(open: "job:roll", answers: ["roll_q": "Update the last two now"])
        case "orb-idle", "orb-working", "orb-needs", "orb-failed", "orb-answers": return JobSetup(orb: true)
        case "jobs-answers": return JobSetup()
        case "job-receipts": return JobSetup(open: "job:site")
        case "job-not-read": return JobSetup(open: "job:trip")
        case "jobs-fenced": return JobSetup()
        case "job-fenced": return JobSetup(open: "job:settings")
        case "answer-weather": return JobSetup(open: "job:wx")
        case "answer-nightly": return JobSetup(open: "job:week")
        case "answer-laptops": return JobSetup(open: "job:laptops")
        case "orb-talking": return JobSetup(orb: true, level: 0.6)
        // A review that needs you and a full weather answer, both with big cards (the crowded panel).
        case "declutter-board": return JobSetup(seen: [])
        case "declutter-weather": return JobSetup(open: "job:dwx", seen: [])
        case "declutter-events": return JobSetup(open: "job:devents", seen: [])
        case "declutter-three": return JobSetup(open: "job:dplan", seen: [])
        case "declutter-many": return JobSetup(seen: [])
        // A finished events report that brought one incidental picture along: an answer, not a review.
        case "events-board": return JobSetup(seen: [])
        case "answer-events": return JobSetup(open: "job:wevents", seen: [])
        case "answer-matchup": return JobSetup(open: "job:match", seen: [])
        // The router question set aside ("Not needed"): it leaves Needs you for Done today.
        case "jobs-set-aside": return JobSetup(setAside: ["router_q"])
        case "job-set-aside": return JobSetup(open: "job:router", setAside: ["router_q"])
        default: return nil
        }
    }

    /// Apply a job fixture to a client: show the state, then open the job or fold into the orb.
    @MainActor
    static func showJobFixture(_ name: String, on c: NativeVoiceClient) -> Bool {
        guard let setup = jobSetup(name), let (state, _) = jobState(name) else { return false }
        for id in jobQuestionTasks { c.model.answers[id] = nil; c.model.dismissedViewTasks.remove(id) }
        for (id, text) in setup.answers { c.model.answers[id] = text }
        c.model.setAsideTasks = Set(setup.setAside)
        c.model.expandedReports = []
        c.model.seenAnswers = setup.seen
        c.model.slim = setup.orb
        c.showPreview(state, workExpanded: false)
        c.model.orbLevel = setup.level
        if let open = setup.open { c.model.onOpenJob(open) } else { c.model.onOpenJob(nil) }
        c.model.boardFocusID = nil
        return true
    }

    static func jobState(_ name: String) -> (VoiceState, workExpanded: Bool)? {
        guard let setup = jobSetup(name) else { return nil }
        let now = Date()
        var s = VoiceState()
        s.now = now
        s.connection = .live
        s.interactionID = "vi_preview"
        s.exchange = Exchange(you: "What needs me?", assistant: "Three things need you; two are still running.", replyStarted: true)

        // Sample times never fall before today's midnight: just after midnight, "40 min ago" was
        // yesterday and the Done-today samples dropped off the board.
        let midnight = Calendar.current.startOfDay(for: now).timeIntervalSince1970
        func sameDay(_ t: TimeInterval) -> TimeInterval { max(t, midnight + 1) }

        func task(_ id: String, job: (String, String)?, title: String, status: String = "completed", age: TimeInterval,
                  short: String? = nil, detail: String? = nil, plan: [String] = [], milestones: [String] = [],
                  result: [String: Any]? = nil, failure: String? = nil) -> TaskItem {
            var json: [String: Any] = ["task_id": id, "run_id": "run_\(id)", "status": status, "title": title,
                                       "updated_at": sameDay(now.timeIntervalSince1970 - age), "status_source": "authored"]
            if let job { json["job"] = ["id": job.0, "name": job.1] }
            if let short { json["short_status"] = short }
            if let detail { json["detail"] = detail }
            if !plan.isEmpty { json["plan"] = plan }
            json["events"] = [["kind": "request", "text": title, "at": now.timeIntervalSince1970 - age - 120]]
                + milestones.enumerated().map { i, m in
                    ["kind": "milestone", "text": m, "at": now.timeIntervalSince1970 - age - Double(90 - i * 20)] as [String: Any] }
            if let result { json["result"] = result }
            if let failure { json["failure"] = ["kind": "error", "text": failure] }
            return TaskItem(json: json)!
        }

        let rel = ("rel", "Release v2.4")
        let night = ("night", "Nightly jobs")
        let roll = ("roll", "App rollout")
        let router = ("router", "Router setting")

        // Release: tests pass, the plugin and the app can ship together or one after the other.
        let relQuestion: [String: Any] = [
            "kind": "question", "question": "Ship the plugin now, or wait and ship both together?",
            "context": "Tests and the offline checks passed. The Mac app has a new board you haven't tried yet.",
            "why": "The plugin change is small and can be rolled back; the app is the part worth a look first.",
            "options": [
                ["text": "Ship the plugin now, the app after you try it", "detail": "Plugin goes out today; the app waits for your OK.",
                 "tags": [["text": "Safest", "tone": "good"]], "risk": "low", "reversible": true],
                ["text": "Ship both now", "detail": "Everyone gets the new board before you've used it.", "risk": "medium", "reversible": true],
                ["text": "Hold both", "detail": "Nothing changes until you say so."]],
            "recommended": 0, "affects": "2 downloads · the public site"]
        let relFlow: [String: Any] = ["kind": "flow", "title": "Where it goes", "nodes": [
            ["label": "Code", "detail": "main", "tone": "good"], ["label": "Checks", "detail": "all passed", "tone": "good"],
            ["label": "Plugin", "detail": "v2.4", "tone": "info"], ["label": "Mac app", "detail": "waits", "tone": "warn"]]]
        var relTasks = [
            task("rel_build", job: rel, title: "Build and test v2.4", age: 1800,
                 result: ["spoken": "Built and tested.", "label": "Built and tested"]),
            task("rel_q", job: rel, title: "Prepare the release", age: 240, plan: ["Build", "Run tests", "Offline checks", "Write notes"],
                 milestones: ["Built", "Tests passed", "Offline checks passed", "Release notes written"],
                 result: ["spoken": "Ready to ship. Plugin now and the app after you try it?", "label": "Ready to ship",
                          "views": [relQuestion, relFlow]]),
        ]
        if setup.answers["rel_q"] != nil {
            relTasks.append(task("rel_ship", job: rel, title: "Ship the plugin", age: 30, result: [
                "spoken": "The plugin is out. The app waits for you.",
                "full": "Shipped plugin v2.4. Tests: 248 passed. Offline checks: ok. The Mac app build is ready for you to try; say ship the app when you're happy.",
                "label": "Plugin shipped",
                "shape": ["type": "ship", "headline": "Plugin v2.4 is out; the app waits for you",
                          "checks": [["label": "tests passed", "value": "248", "good": true], ["label": "offline checks", "value": "OK", "good": true],
                                     ["label": "app build", "value": "Ready", "good": true]],
                          "shipped": ["Plugin v2.4 on the download page", "Release notes"],
                          "open": ["Try the new board, then say \u{201C}ship the app\u{201D}"], "undo": true]]))
        }

        // Nightly: two jobs failed overnight; a fix is ready.
        let nightQuestion: [String: Any] = [
            "kind": "question", "question": "Keep the two overnight fixes?",
            "context": "The backup and the report job both ran into the same lock at 2 am. Each now waits for the other.",
            "why": "Both fixes only change the start time, and last night's rerun passed with them.",
            "options": [
                ["text": "Keep both fixes", "detail": "Backup moves to 1:30, the report to 3:00.",
                 "tags": [["text": "Tested last night", "tone": "good"]], "risk": "low", "reversible": true],
                ["text": "Keep the backup fix only", "detail": "The report may still clash.", "risk": "medium", "reversible": true],
                ["text": "Undo both"]],
            "recommended": 0, "affects": "2 scheduled jobs", "approvable": true]
        let lanes: [String: Any] = ["kind": "lanes", "title": "Last night", "ticks": ["12a", "1a", "2a", "3a", "4a", "5a"],
                                    "lanes": [
                                        ["label": "Backup", "segments": [["from": 0.30, "to": 0.42, "tone": "bad", "label": "Locked"],
                                                                         ["from": 0.62, "to": 0.70, "tone": "good", "label": "Rerun"]]],
                                        ["label": "Report", "segments": [["from": 0.38, "to": 0.45, "tone": "bad"],
                                                                         ["from": 0.72, "to": 0.80, "tone": "good"]]],
                                        ["label": "Cleanup", "segments": [["from": 0.05, "to": 0.12, "tone": "good"]]],
                                        ["label": "Sync", "segments": [["from": 0.15, "to": 0.25, "tone": "good"], ["from": 0.85, "to": 0.92, "tone": "good"]]]],
                                    "note": "Red: both jobs waited on the same lock at 2:00."]
        var nightTasks = [
            task("night_q", job: night, title: "Check last night's jobs", age: 600,
                 result: ["spoken": "Two jobs failed on the same lock; I have a fix.", "label": "2 failures found",
                          "views": [nightQuestion, lanes]]),
        ]
        if setup.answers["night_q"] != nil {
            nightTasks.append(task("night_apply", job: night, title: "Apply the two fixes", status: "working", age: 8,
                                   short: "Moving the report job to 3:00", plan: ["Move the backup", "Move the report", "Dry run", "Confirm tonight's schedule"],
                                   milestones: ["Backup moved to 1:30"]))
        }

        // Rollout: the app update reached most devices.
        let devices: [String: Any] = ["kind": "devices", "title": "Devices", "items": [
            ["name": "Laptop", "value": "2.4", "tone": "good", "history": [1, 1, 2, 2, 3, 4]],
            ["name": "Desktop", "value": "2.4", "tone": "good", "history": [2, 2, 2, 3, 3, 3]],
            ["name": "Tablet", "value": "2.3", "tone": "warn", "detail": "Asleep"],
            ["name": "Studio", "value": "2.3", "tone": "warn", "detail": "Needs a restart"]]]
        let rollQuestion: [String: Any] = [
            "kind": "question", "question": "Update the last two devices now?",
            "context": "Two of four devices are on the new version. The other two are asleep or need a restart.",
            "why": "Nothing is running on them now, so a restart costs nothing.",
            "options": [
                ["text": "Update the last two now", "detail": "Restarts the app on both.", "risk": "low", "reversible": true],
                ["text": "Wait until tonight", "detail": "They update on their own at 2 am."]],
            "recommended": 0, "approvable": true]
        var rollTasks = [
            task("roll_q", job: roll, title: "Roll out the app update", age: 400, result: [
                "spoken": "Two of four devices are updated.", "label": "2 of 4 updated", "views": [rollQuestion, devices]]),
        ]
        if setup.answers["roll_q"] != nil {
            var after = devices
            after["items"] = [["name": "Laptop", "value": "2.4", "tone": "good"], ["name": "Desktop", "value": "2.4", "tone": "good"],
                              ["name": "Tablet", "value": "2.4", "tone": "good"], ["name": "Studio", "value": "2.4", "tone": "good"]]
            let before: [String: Any] = ["kind": "before_after", "title": "Version", "before": ["label": "Before", "text": "2 of 4 on 2.4"],
                                         "after": ["label": "After", "text": "All 4 on 2.4"]]
            rollTasks.append(task("roll_done", job: roll, title: "Update the last two devices", age: 20,
                                  plan: ["Wake the tablet", "Restart the studio app", "Install 2.4", "Check versions"],
                                  milestones: ["Tablet awake", "Studio app restarted", "2.4 installed on both", "All four report 2.4"],
                                  result: ["spoken": "All four devices are on 2.4.", "label": "All updated",
                                           "full": "Woke the tablet, restarted the studio app, installed 2.4 on both and checked all four devices report 2.4.",
                                           "views": [after, before],
                                           "shape": ["type": "rollout", "headline": "All four devices are on 2.4",
                                                     "checks": [["label": "devices updated", "value": "4 of 4", "good": true],
                                                                ["label": "errors", "value": "0", "good": true]],
                                                     "undo": true]]))
        }

        let routerQuestion: [String: Any] = [
            "kind": "question", "question": "When should the router stop sending work to a busy machine?",
            "options": [["text": "After 1 error, for 30 seconds", "risk": "low", "reversible": true],
                        ["text": "After 3 errors, for 30 seconds", "risk": "medium", "reversible": true], "Leave it"],
            "recommended": 0, "approvable": true]
        let routerTask = task("router_q", job: router, title: "Tune the router", age: 900,
                              result: ["spoken": "Which retry setting?", "label": "Setting to pick", "views": [routerQuestion]])
        let indexTask = task("index", job: ("index", "Search index rebuild"), title: "Rebuild the search index", status: "working", age: 12,
                             short: "Indexing documents, 61% done")
        let weather = task("weather", job: nil, title: "Weather tomorrow", age: 3000, result: ["spoken": "Sunny, 72.", "label": "Sunny, 72"])
        let notes = task("notes", job: ("notes", "Meeting notes"), title: "Tidy the meeting notes", age: 2500,
                         result: ["spoken": "Done.", "label": "Notes tidied"])
        let rollRunning = task("roll_run", job: ("app", "Docs refresh"), title: "Refresh the docs", status: "working", age: 6,
                               short: "Running the link checker", plan: ["Pull changes", "Rebuild pages", "Check links", "Publish preview", "Report"],
                               milestones: ["Pulled changes", "Pages rebuilt"])

        // Answers: information brought back, nothing to decide.
        let wxCard: [String: Any] = ["kind": "weather", "place": "Riverside", "temp": 22, "condition": "Sunny", "icon": "sun",
                                     "high": 23, "low": 14, "days": [
                                        ["label": "Sat", "icon": "sun", "high": 23, "low": 14],
                                        ["label": "Sun", "icon": "rain", "high": 17, "low": 12, "rain": 80]]]
        let answerWeather = task("ans_wx", job: ("wx", "Weekend weather"), title: "Weekend weather", age: 420, result: [
            "spoken": "Saturday is the better day: sunny and 22 degrees. Sunday turns to rain after noon.",
            "full": "Saturday is the better day: sunny, 22° with a light breeze, 14° overnight. Sunday clouds over in the morning and rain is likely after noon (80%), with a high of 17°.\n\nForecast from [the weather service](https://weather.example.com/riverside).",
            "label": "Saturday is the drier day", "views": [wxCard]])
        let weekStats: [String: Any] = ["kind": "stats", "title": "This week", "items": [
            ["label": "Runs", "value": "28"], ["label": "Passed", "value": "26", "good": true],
            ["label": "Failed", "value": "2", "good": false], ["label": "Avg time", "value": "14 min"]]]
        let weekChart: [String: Any] = ["kind": "chart", "title": "Runs per night", "style": "bar",
                                        "labels": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                                        "series": [["name": "Runs", "values": [4, 4, 4, 3, 4, 5, 4]]]]
        let answerWeek = task("ans_week", job: ("week", "Nightly jobs this week"), title: "How did the nightly jobs do this week",
                              age: 900, result: [
            "spoken": "26 of 28 nightly runs passed this week. The two failures were a timeout and a lock, both rerun fine.",
            "full": "26 of 28 nightly runs passed this week. Thursday's backup timed out once and Saturday's report job hit a lock; both reran fine the same night. Average run time was 14 minutes.",
            "label": "Nightly jobs checked", "views": [weekStats, weekChart],
            "shape": ["type": "answer", "headline": "26 of 28 nightly runs passed this week",
                      "sections": [["title": "The two failures", "points": ["Thursday: the backup timed out once, rerun passed",
                                                                            "Saturday: the report job hit a lock, rerun passed"]]]]])
        let compare: [String: Any] = ["kind": "comparison", "title": "Laptop A vs Laptop B", "columns": ["Laptop A", "Laptop B"],
                                      "rows": [["label": "Price", "values": ["$1,299", "$1,099"]],
                                               ["label": "Weight", "values": ["2.6 lb", "3.5 lb"]],
                                               ["label": "Battery", "values": ["18 h", "12 h"]],
                                               ["label": "Screen", "values": ["13.6 in", "14.2 in"]]],
                                      "winner": "Laptop A"]
        let answerLaptops = task("ans_laptops", job: ("laptops", "Two laptops compared"), title: "Compare the two laptops", age: 200, result: [
            "spoken": "Laptop A is the better fit: lighter, with six more hours of battery, for 200 dollars more.",
            "full": "Laptop A is the better fit for carrying around: 2.6 lb against 3.5 lb and about 18 hours of battery against 12. Laptop B costs $200 less and has a slightly bigger screen.\n\nSpecs from [Laptop A's spec page](https://laptops.example.com/a) and [Laptop B's spec page](https://laptops.example.com/b).",
            "label": "Laptop A fits you better", "views": [compare]])

        // Declutter: the user's crowded call, generic data. A review with one large picture, and a
        // weather answer with the whole forecast (hours and seven days).
        let fullWeather: [String: Any] = [
            "kind": "weather", "place": "Riverside", "temp": 78, "condition": "Clear", "icon": "sun", "high": 81, "low": 66,
            "hours": (0..<6).map { i in ["label": ["Now", "1p", "2p", "3p", "4p", "5p"][i], "icon": i < 4 ? "sun" : "cloud.sun",
                                         "temp": [78, 79, 80, 81, 80, 78][i]] as [String: Any] },
            "days": zip(["Today", "Sat", "Sun", "Mon", "Tue", "Wed", "Thu"], [(81, 66), (83, 68), (79, 65), (74, 61), (72, 60), (76, 62), (78, 64)])
                .map { ["label": $0.0, "icon": $0.1.0 > 78 ? "sun" : "cloud.sun", "high": $0.1.0, "low": $0.1.1] as [String: Any] }]
        let declutterWeather = task("d_wx", job: ("dwx", "Weekend weather forecast"), title: "Weekend weather forecast", age: 300, result: [
            "spoken": "Clear and 78 now. Saturday is the warmest day at 83; it cools off from Monday.",
            "full": "Clear and 78° now, topping out at 81° this afternoon. Saturday is the warmest day (83°/68°), Sunday a little cooler. From Monday it drops to the low 70s.",
            "label": "Warm and clear all weekend", "views": [fullWeather],
            "shape": ["type": "answer", "headline": "Warm and clear all weekend, 83° on Saturday"]])
        var eventsJSON: [String: Any] = ["task_id": "d_events", "run_id": "run_d_events", "status": "completed",
                                         "title": "Weekend events in the city", "updated_at": now.timeIntervalSince1970 - 60,
                                         "status_source": "authored", "job": ["id": "devents", "name": "Cool events this weekend"],
                                         "review": ["images": [1], "settled_at": now.timeIntervalSince1970 - 60]]
        eventsJSON["result"] = ["spoken": "I found six events and saved the listings page for you to look at.",
                                "full": "Six events this weekend: a night market on Friday, two gallery openings and a jazz brunch on Saturday, and a river walk and a film screening on Sunday.",
                                "label": "Pictures to look at",
                                "cards": [["kind": "image", "name": "events-listing.png"]]]
        let declutterEvents = TaskItem(json: eventsJSON)!
        // A finished research report (shape with sections) that also saved one picture of a listings
        // page: the picture is incidental, the report is the answer.
        var weekendJSON: [String: Any] = ["task_id": "w_events", "run_id": "run_w_events", "status": "completed",
                                          "title": "Weekend events", "updated_at": now.timeIntervalSince1970 - 45,
                                          "status_source": "authored", "job": ["id": "wevents", "name": "Weekend events"],
                                          "review": ["images": [1], "settled_at": now.timeIntervalSince1970 - 45]]
        weekendJSON["result"] = [
            "spoken": "Plenty going on: a night market Friday, a gallery opening and a jazz brunch Saturday, and a river walk Sunday. I'd do the brunch and the night market.",
            "full": "Here's what's on this weekend.\n\nAll weekend: a riverside art fair, 10:00 to 18:00, free.\n\nSaturday: [jazz brunch at the Corner Café](https://www.example.com/jazz-brunch) at 11:00 ($25); gallery opening at North Hall at 15:00 (free); night market at Pier 4 from 19:00.\n\nSunday: guided river walk at 10:00 (free); outdoor film screening at the park at 20:00.\n\nMy pick: the jazz brunch, then the night market.",
            "label": "Six events this weekend",
            "cards": [["kind": "image", "name": "listings-home.png"]],
            "shape": ["type": "research", "headline": "Six good events this weekend; the jazz brunch and night market stand out",
                      "sections": [["title": "All weekend", "points": ["Riverside art fair, 10:00–18:00, free"]],
                                   ["title": "Saturday", "points": ["[Jazz brunch](https://www.example.com/jazz-brunch), Corner Café, 11:00 ($25)", "Gallery opening, North Hall, 15:00",
                                                                    "Night market, Pier 4, from 19:00"]],
                                   ["title": "Sunday", "points": ["Guided river walk, 10:00, free", "Outdoor film screening, 20:00"]]],
                      "open": ["Brunch tables book up by Friday"]]]
        let weekendEvents = TaskItem(json: weekendJSON)!
        // A matchup answer the plugin drew as a projected scoreboard, plus an incidental screenshot.
        var matchJSON: [String: Any] = ["task_id": "m_match", "run_id": "run_m_match", "status": "completed",
                                        "title": "This week's matchup", "updated_at": now.timeIntervalSince1970 - 30,
                                        "status_source": "authored", "job": ["id": "match", "name": "This week's matchup"],
                                        "review": ["images": [1], "settled_at": now.timeIntervalSince1970 - 30]]
        matchJSON["result"] = [
            "spoken": "You're projected to win, 118.4 to 97.2, about a 64% chance.",
            "full": "You're projected to win 118.4 to 97.2, about a 64% chance. Your top scorer is projected at 24.6.\n\nStill open: check Friday's injury report for your second receiver.",
            "label": "Projected win",
            "cards": [["kind": "image", "name": "league-page.png"]],
            "views": [["kind": "game", "league": "League", "status": "projected", "detail": "64% win chance",
                       "teams": [["name": "Your team", "score": "118.4", "winner": true],
                                 ["name": "Opponent", "score": "97.2", "winner": false, "record": "1-4"]]]],
            "shape": ["type": "answer", "headline": "Projected win, 118.4 to 97.2",
                      "sections": [["title": "Matchup outlook", "points": ["About a 64% chance to win", "Top scorer projected at 24.6"]]],
                      "open": ["Check Friday's injury report for your second receiver"]]]
        let matchup = TaskItem(json: matchJSON)!
        let planPlaces: [String: Any] = ["kind": "list", "title": "Saturday plan", "items": [
            ["text": "Jazz brunch", "detail": "11:00 · Corner Café", "url": "https://www.example.com/jazz-brunch"], ["text": "Gallery opening", "detail": "15:00 · North Hall"],
            ["text": "Night market", "detail": "19:00 · Pier 4", "url": "https://www.example.com/night-market"]]]
        let planAgenda: [String: Any] = ["kind": "stats", "title": "Saturday at a glance", "items": [
            ["label": "Stops", "value": "3"], ["label": "Walking", "value": "2.1 km"],
            ["label": "Warmest", "value": "83°"], ["label": "Cost", "value": "$45"]]]
        let declutterPlan = task("d_plan", job: ("dplan", "Saturday plan"), title: "Plan Saturday", age: 120, result: [
            "spoken": "Here's a plan for Saturday: brunch, a gallery, then the night market.",
            "full": "Brunch at 11, the gallery opening at 3 and the night market from 7. It stays clear all day, 83° at the warmest.",
            "label": "Saturday planned", "views": [planPlaces, planAgenda, fullWeather]])

        // Follow-ups and their receipts: queued (Hermes took it, not read yet), read, and not read.
        func withFollowUps(_ base: TaskItem, _ items: [(String, String)], age: TimeInterval) -> TaskItem {
            var json: [String: Any] = ["task_id": base.id, "run_id": base.info.runID ?? "", "status": base.info.status,
                                       "title": base.info.title ?? "", "updated_at": now.timeIntervalSince1970 - age,
                                       "updated": now.timeIntervalSince1970 - 1, "status_source": "authored"]
            if let j = base.info.job { json["job"] = ["id": j.id, "name": j.name] }
            if let short = base.info.shortStatus { json["short_status"] = short }
            if let result = base.info.result { json["result"] = ["spoken": result.spoken ?? "", "full": result.full ?? "", "label": result.label ?? ""] }
            var events: [[String: Any]] = base.info.events.map { ["kind": $0.kind, "text": $0.text, "at": $0.at?.timeIntervalSince1970 ?? 0] }
            json["follow_ups"] = items.enumerated().map { i, item -> [String: Any] in
                events.append(["kind": "milestone", "text": "You added: \(item.0)", "at": now.timeIntervalSince1970 - age + Double(i * 20)])
                return ["text": item.0, "milestone": "You added: \(item.0)", "state": item.1, "at": now.timeIntervalSince1970 - age]
            }
            json["events"] = events
            return TaskItem(json: json)!
        }
        let siteRun = withFollowUps(task("site", job: ("site", "Site copy update"), title: "Update the pricing page copy", status: "working",
                                         age: 30, short: "Editing the pricing section", milestones: ["Read the current page"]),
                                    [("keep the old prices in a footnote", "landed"), ("and use the shorter headline", "queued")], age: 90)
        let tripDone = withFollowUps(task("trip", job: ("trip", "Lisbon trip plan"), title: "Plan a four day trip to Lisbon", age: 40,
                                          milestones: ["Picked the neighborhoods"],
                                          result: ["spoken": "Here's a four day plan.", "full": "Day one in Alfama, day two in Belém, day three a trip to Sintra, day four in Chiado.",
                                                   "label": "Four days planned"]),
                                     [("fewer museums", "not_read")], age: 60)
        let fixRun = task("fix", job: ("fix", "Checkout bug fix"), title: "Fix the checkout bug in the shop repo", status: "working",
                          age: 8, short: "Running the checkout tests", milestones: ["Found the failing test"])
        var fencedJSON: [String: Any] = ["task_id": "settings", "status": "waiting_on_task", "title": "Refactor the settings page",
                                         "updated": now.timeIntervalSince1970 - 1, "updated_at": now.timeIntervalSince1970 - 20,
                                         "short_status": "Waiting for another task",
                                         "detail": "Starts when \"Fix the checkout bug in the shop repo\" finishes (same project).",
                                         "status_source": "system", "job": ["id": "settings", "name": "Settings page refactor"],
                                         "waiting": ["name": "Fix the checkout bug in the shop repo", "project": "shop"]]
        fencedJSON["events"] = [["kind": "request", "text": "Refactor the settings page in the shop repo", "at": now.timeIntervalSince1970 - 25],
                                ["kind": "milestone", "text": "Waiting for \"Fix the checkout bug in the shop repo\" (shop)", "at": now.timeIntervalSince1970 - 20]]
        let fenced = TaskItem(json: fencedJSON)!

        switch name {
        case "job-receipts":
            s.exchange = Exchange(you: "And use the shorter headline", assistant: "Lined up for the pricing page task.", replyStarted: true)
            s.tasks = [weather, siteRun]
        case "job-not-read":
            s.exchange = Exchange(you: "Fewer museums", assistant: "It finished before it read that. Want me to start it as new work?", replyStarted: true)
            s.tasks = [weather, tripDone]
        case "jobs-fenced", "job-fenced":
            s.exchange = Exchange(you: "Refactor the settings page in the shop repo",
                                  assistant: "The checkout fix is already working on shop, so this starts when it's done.", replyStarted: true)
            s.tasks = [weather, fixRun, fenced]
        case "declutter-board", "declutter-weather", "declutter-events":
            s.exchange = Exchange(you: "What's the weather this weekend?", assistant: "Warm and clear; it's on your board.", replyStarted: true)
            s.tasks = [declutterWeather, declutterEvents]
        case "declutter-three":
            s.exchange = Exchange(you: "Plan Saturday for me.", assistant: "Done, it's on your board.", replyStarted: true)
            s.tasks = [declutterWeather, declutterEvents, declutterPlan]
        case "declutter-many":
            // More jobs than the panel can show at once: the board has to scroll.
            s.tasks = [declutterWeather, declutterEvents, declutterPlan, answerWeek, answerLaptops, routerTask, indexTask, rollRunning]
                + nightTasks + rollTasks + relTasks
        case "answer-matchup":
            s.tasks = [rollRunning, matchup]
        case "events-board", "answer-events":
            s.exchange = Exchange(you: "Anything cool going on this weekend?", assistant: "Six good ones; it's on your board.", replyStarted: true)
            s.tasks = [rollRunning, weekendEvents]
        case "orb-answers":
            s.tasks = [weather, answerWeather]
        case "jobs-answers", "answer-weather", "answer-nightly", "answer-laptops":
            s.exchange = Exchange(you: "How did the nightly jobs do?", assistant: "26 of 28 passed. It's on your board.", replyStarted: true)
            s.tasks = [notes, routerTask, answerWeek, indexTask, answerWeather, answerLaptops, rollRunning]
        case "orb-idle":
            s.tasks = [weather]
        case "orb-talking":
            s.tasks = [weather]
            s.exchange = Exchange(you: "Can you check on", assistant: "", replyStarted: false)
        case "orb-working":
            s.tasks = [weather, rollRunning]
        case "orb-needs":
            s.tasks = [weather, rollRunning, routerTask] + relTasks + [answerWeather]
        case "orb-failed":
            s.tasks = [weather, task("sync_fail", job: ("sync", "Photo sync"), title: "Sync the photos", status: "failed", age: 60,
                                     failure: "The photo library was locked by another app.")]
        default:
            s.tasks = [weather, notes, routerTask] + nightTasks + [indexTask] + rollTasks + [rollRunning] + relTasks
        }
        s.workInfo = s.tasks.last?.info
        s.runID = s.workInfo?.runID
        s.work = deriveWork(s, now: now)
        return (s, false)
    }
}
