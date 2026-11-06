import AppKit
import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

/// The history window: a normal, resizable Mac window (it can stay open beside the panel, also
/// during a call; only the mic button hides then).
@MainActor
final class HistoryWindowController: NSObject, NSWindowDelegate {
    let store = HistoryStore()
    private(set) var window: NSWindow?

    func show() {
        let w = window ?? makeWindow()
        window = w
        store.start()
        NSApp.activate(ignoringOtherApps: true)
        w.makeKeyAndOrderFront(nil)
    }

    var isVisible: Bool { window?.isVisible == true }

    private func makeWindow() -> NSWindow {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 700),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                         backing: .buffered, defer: false)
        w.title = "Speakeasy History"
        w.titlebarAppearsTransparent = true
        w.titleVisibility = .hidden
        w.isMovableByWindowBackground = true
        // Follows the app's appearance (Settings › General › Appearance).
        w.backgroundColor = NSColor(name: nil) { a in
            a.bestMatch(from: [.darkAqua, .aqua]) == .aqua ? NSColor(calibratedRed: 0.984, green: 0.973, blue: 0.949, alpha: 1)
                : NSColor(calibratedRed: 0.118, green: 0.110, blue: 0.102, alpha: 1)
        }
        w.minSize = NSSize(width: 720, height: 480)
        w.isReleasedWhenClosed = false
        let host = NSHostingView(rootView: HistoryRootView(store: store))
        host.sizingOptions = []   // the window keeps its size; the lists scroll inside it
        w.contentView = host
        w.center()
        w.setFrameAutosaveName("SpeakeasyHistory")
        w.delegate = self
        return w
    }

    func windowWillClose(_ notification: Notification) { store.stop() }
    func windowDidBecomeKey(_ notification: Notification) {
        // Back in front: pick up what changed while it was away.
        if store.supported, store.backend != nil, !store.list.loading { store.refresh() }
        if let id = store.target?.sessionID { store.load(id) }
    }
}

extension Notification.Name {
    /// Open the History window; `object` is a session id to open, or nil.
    static let speakeasyOpenHistory = Notification.Name("speakeasy.openHistory")
}

// MARK: - Demo (screenshots; invented, work-first data, in the server's JSON shapes)

@MainActor
final class DemoHistoryBackend: HistoryBackend {
    var sessions: [HistorySession]
    var conversations: [String: HistoryConversation]
    var sent: [HistoryMessageRequest] = []
    var answers: [(String, String, String)] = []

    init(now: Date = Date()) {
        (sessions, conversations) = DemoHistory.make(now: now)
    }

    func page(_ query: HistoryQuery) async throws -> HistoryPage {
        var all = sessions
        if let source = query.source { all = all.filter { HistorySources.filterID(for: $0.source) == source || $0.source == source } }
        else { all = all.filter { query.shown.lists($0) } }
        let q = query.q.lowercased()
        if !q.isEmpty {
            all = all.compactMap { s in
                if s.title.lowercased().contains(q) { return s }
                guard let hit = conversations[s.id]?.entries.first(where: { $0.text.lowercased().contains(q) }) else { return nil }
                var m = s; m.match = DemoHistory.snippet(hit.text, around: q); return m
            }
        }
        let start = Int(query.cursor ?? "0") ?? 0
        let slice = Array(all.dropFirst(start).prefix(query.limit))
        let next = start + slice.count < all.count ? String(start + slice.count) : nil
        let counts = Dictionary(grouping: sessions.compactMap { HistorySources.filterID(for: $0.source) }, by: { $0 }).mapValues(\.count)
        return HistoryPage(sessions: slice, nextCursor: next, filters: HistoryList.defaultFilters,
                           sources: HistoryList.defaultFilters.compactMap { f in
                               guard !query.shown.hides(filter: f.id) else { return nil }
                               return counts[f.id].map { HistorySourceCount(source: f.id, count: $0) }
                           },
                           automated: query.shown.facet(for: sessions))
    }

    func conversation(_ sessionID: String) async throws -> HistoryConversation {
        guard let c = conversations[sessionID] else { throw ServerClient.HTTPError(status: 404, message: "I couldn't find that conversation.") }
        return c
    }

    func send(_ request: HistoryMessageRequest) async throws -> HistoryMessageReply {
        sent.append(request)
        return HistoryMessageReply(via: .task, runID: "run_demo")
    }

    var models = ModelCatalog.sample
    var sessionModels: [SessionModelRequest] = []
    func models() async throws -> ModelCatalog { models }
    func skills() async throws -> [SlashSkill] { DemoSlashSkills.all }
    func setSessionModel(_ request: SessionModelRequest) async throws {
        sessionModels.append(request)
        conversations[request.sessionID]?.modelChoice = request.choice.isEmpty ? nil : request.choice
    }
    func setDefaultModel(_ defaults: JobModelDefaults) async throws {
        models.defaultChoice = ModelChoice(model: defaults.model, reasoning: defaults.reasoning)
    }

    var roster = AgentRoster.sample
    var startedAgents: [String?] = []
    func agents() async throws -> AgentRoster { roster }

    func startChat(text: String, clientID: String, choice: ModelChoice?, agent: String?) async throws -> String? {
        startedAgents.append(agent)
        let id = "s_new_\(sessions.count)", run = "run_new_\(sessions.count)"
        let s = HistorySession(id: id, title: text, source: "speakeasy", sourceLabel: "Speakeasy", lastActivity: Date(),
                               preview: text, state: .running, runIDs: [run])
        sessions.insert(s, at: 0)
        conversations[id] = HistoryConversation(id: id, title: text, source: "speakeasy", sourceLabel: "Speakeasy", state: .running,
                                                sendVia: .task, entries: [HistoryEntry(id: "m0", kind: .user, at: Date(), text: text)])
        return run
    }

    func answerApproval(sessionID: String, runID: String, choice: String) async throws { answers.append((sessionID, runID, choice)) }

    /// ✕ / Undo, as a real Hermes records it: the job's question closes (a quiet line) or reopens.
    var dismissedQuestions: [String] = []
    func dismissQuestion(runID: String, undo: Bool) async throws {
        dismissedQuestions.append((undo ? "undo:" : "") + runID)
        for (id, var c) in conversations {
            guard let i = c.jobs.firstIndex(where: { $0.runID == runID }) else { continue }
            c.jobs[i].questionClosed = undo ? nil : "dismissed"
            c.jobs[i].setAside = !undo
            c.jobs[i].questionOpen = undo
            conversations[id] = c
        }
    }
}

enum DemoHistory {
    static func snippet(_ text: String, around q: String) -> String {
        let flat = HistoryLook.oneLine(text)
        guard let r = flat.range(of: q, options: .caseInsensitive) else { return String(flat.prefix(90)) }
        let start = flat.index(r.lowerBound, offsetBy: -30, limitedBy: flat.startIndex) ?? flat.startIndex
        return (start == flat.startIndex ? "" : "…") + String(flat[start...].prefix(100))
    }

    static func make(now: Date) -> ([HistorySession], [String: HistoryConversation]) {
        let cal = Calendar.current
        func ago(_ minutes: Double) -> Double { now.timeIntervalSince1970 - minutes * 60 }
        func daysAgo(_ d: Int, _ hour: Int, _ minute: Int = 0) -> Double {
            let day = cal.date(byAdding: .day, value: -d, to: now)!
            return cal.date(bySettingHour: hour, minute: minute, second: 0, of: day)!.timeIntervalSince1970
        }
        func user(_ id: String, _ at: Double, _ text: String) -> [String: Any] { ["kind": "user", "id": id, "at": at, "text": text] }
        func bot(_ id: String, _ at: Double, _ text: String) -> [String: Any] { ["kind": "assistant", "id": id, "at": at, "text": text] }
        func tools(_ id: String, _ items: [[String: Any]]) -> [String: Any] { ["kind": "tools", "id": id, "items": items] }
        func call(_ n: Int, _ kind: String, _ text: String, _ tool: String, _ start: Double, _ took: Double?,
                  args: String? = nil, result: String? = nil, failed: Bool = false) -> [String: Any] {
            var o: [String: Any] = ["id": "c\(n)", "n": n, "kind": kind, "text": text, "tool": tool, "started_at": start,
                                    "status": took == nil ? "running" : failed ? "failed" : "done"]
            if let took { o["ended_at"] = start + took }
            if let args { o["args"] = args }
            if let result { o["result"] = result }
            return o
        }

        var sessions: [HistorySession] = []
        var convs: [String: HistoryConversation] = [:]
        func add(_ id: String, _ title: String, _ source: String, _ label: String, _ last: Double, _ preview: String,
                 state: String = "idle", entries: [[String: Any]] = [], jobs: [[String: Any]] = [], approvals: [[String: Any]] = [],
                 pinned: Bool = false, unread: Bool = false, more: Bool = false, extra: [String: Any] = [:]) {
            var row: [String: Any] = ["id": id, "title": title, "source": source, "source_label": label, "last_activity": last,
                                      "preview": preview, "state": state, "pinned": pinned, "unread": unread,
                                      "run_ids": jobs.compactMap { $0["run_id"] as? String }]
            if source == "kanban" || source == "cron" || source == "subagent" { row["automated"] = true }
            sessions.append(HistorySession(json: row)!)
            var full = row
            full["entries"] = entries; full["jobs"] = jobs; full["approvals"] = approvals; full["has_more"] = more
            full["send_via"] = source == "speakeasy" ? "task" : "linked"
            full.merge(extra) { $1 }
            convs[id] = HistoryConversation(json: ["session": full])
        }

        // A background helper a task started (listed only when the filter menu turns helpers on).
        add("s_helper", "Measure the lantern glass", "subagent", "Helper", ago(18),
            "The glass is 6 in tall and 3.5 in across.", entries: [
                user("m1", ago(19), "Measure the lantern glass and report the height and width in inches."),
                bot("m2", ago(18), "The glass is 6 in tall and 3.5 in across."),
            ])

        // A Kanban board worker, running: its card as the header, its own updates as messages.
        let k0 = ago(26)
        add("s_board", "LAN-12: Lantern wick burns too fast", "kanban", "Kanban", ago(0.2),
            "Running the flame tests after the wick change.", state: "running", entries: [
                ["kind": "note", "id": "m1", "at": k0, "text": "LAN-12 \u{00B7} Lantern wick burns too fast", "task": [
                    "title": "Lantern wick burns too fast", "issue": "LAN-12",
                    "url": "https://tracker.example/acme/issue/LAN-12/lantern-wick-burns-too-fast",
                    "goal": "The demo lantern's wick is gone in ten minutes; make it last an hour at the same flame size.",
                    "instructions": "Tracker issue LAN-12: Lantern wick burns too fast\n\nThe demo lantern's wick is gone in ten minutes. Make it last an hour. Keep the flame the same size.\n\nIntegration bases: from main.\n\n--- Team board at dispatch ---\n- LAN-9 Glass cleaning (in progress)\n- LAN-14 Handle squeaks (queued)"]],
                bot("m2", k0 + 30, "Starting: making the wick last an hour without a smaller flame.").merging(["update": true]) { $1 },
                tools("t1", [call(1, "read", "Read wick.py", "read_file", k0 + 40, 0.4),
                             call(2, "search", "Searched the files for “burn_rate”", "search_files", k0 + 50, 0.9),
                             call(3, "run", "Ran the flame tests", "terminal", k0 + 70, 48, result: "2 failed, 14 passed")]),
                bot("m3", ago(14), "Found it: the burn rate is per second, but the wick length is in minutes, so it burns 60 times too fast. Fixing the units now.").merging(["update": true]) { $1 },
                tools("t2", [call(4, "edit", "Edited wick.py", "patch", ago(12), 0.6),
                             call(5, "edit", "Edited test_wick.py", "patch", ago(11), 0.5),
                             call(6, "run", "Ran the flame tests", "terminal", ago(1), nil)]),
            ], extra: ["live": ["now": "Running the flame tests after the units fix", "so_far": "Read the code, found the units mix-up, edited 2 files"]])
        add("s_board_done", "LAN-9: Glass fogs up when lit", "kanban", "Kanban", ago(70),
            "Done: the vent now opens before the flame heats the glass. 9 tests pass.", entries: [
                ["kind": "note", "id": "m1", "at": ago(110), "text": "LAN-9 \u{00B7} Glass fogs up when lit", "task": [
                    "title": "Glass fogs up when lit", "issue": "LAN-9", "instructions": "Tracker issue LAN-9: Glass fogs up when lit."]],
                bot("m2", ago(70), "Done: the vent now opens before the flame heats the glass. 9 tests pass.").merging(["update": true]) { $1 },
            ])
        add("s_digest", "Morning digest", "cron", "Scheduled", ago(150), "Three things need you today.", entries: [
                ["kind": "note", "id": "m1", "at": ago(151), "text": "Scheduled run"],
                bot("m2", ago(150), "Three things need you today: the staging upload, the pricing question and a calendar clash at 3 PM."),
            ])

        let t0 = ago(9)
        add("s_flaky", "Fix the flaky checkout test", "speakeasy", "Speakeasy", ago(0.5), "Rerunning the checkout test 50 times",
            state: "running", entries: [
                user("m1", t0, "The checkout test keeps failing on CI. Find out why and fix it.").merging(["call": ["lines": [
                    ["who": "you", "text": "The build went red again this morning."],
                    ["who": "agent", "text": "Which part, the checkout test again?"],
                    ["who": "you", "text": "The checkout test keeps failing on CI. Find out why and fix it."]]]]) { $1 },
                tools("t1", [call(1, "search", "Searched the files for “checkout”", "search_files", t0 + 10, 1.8, args: "checkout",
                                  result: "3 matches\ncheckout.spec.ts:12  test('checkout', …)\npayment-mock.ts:1  // payment mock"),
                             call(2, "read", "Read checkout.spec.ts", "read_file", t0 + 14, 0.6, args: "checkout.spec.ts",
                                  result: "41  await page.waitForTimeout(500)\n42  await page.click('#pay')"),
                             call(3, "run", "Ran the test 12 times", "terminal", t0 + 20, 61,
                                  args: "npx playwright test checkout.spec.ts --repeat-each=12",
                                  result: "10 passed, 2 failed (1.2m)\n  checkout.spec.ts:43  Timed out waiting for “Order placed”", failed: true),
                             call(4, "read", "Read payment-mock.ts", "read_file", t0 + 90, 0.5)]),
                bot("m2", ago(6), "It's a race: the test clicks **Pay** before the payment mock is listening, so it fails about 1 run in 6. A fixed 500 ms wait was standing in for “mock ready”."),
                user("m3", ago(5), "Wait on the mock's ready signal instead of the timeout, then run it 50 times.").merging(["voice": true]) { $1 },
                tools("t2", [call(5, "edit", "Edited payment-mock.ts", "patch", ago(4.8), 0.7, args: "payment-mock.ts",
                                  result: "+  export const mockReady = new Promise(r => server.on('listening', r))"),
                             call(6, "edit", "Edited checkout.spec.ts", "patch", ago(4.6), 0.6, args: "checkout.spec.ts",
                                  result: "-  await page.waitForTimeout(500)\n+  await mockReady"),
                             call(7, "run", "Ran the test 50 times", "terminal", ago(4), nil,
                                  args: "npx playwright test checkout.spec.ts --repeat-each=50")]),
                bot("m4", ago(1), "Changed the test to wait for the mock. Running it 50 times now: 31 passes so far, no failures."),
            ], jobs: [["run_id": "run_0012", "status": "running", "title": "Fix the flaky checkout test",
                       "short_status": "Rerunning the checkout test: 31 of 50", "step_index": 3, "step_count": 4,
                       "now_line": "Running the checkout test 50 times · 31 passed so far",
                       "so_far": "Found the fixed 500 ms wait, made the test wait for the mock, started the repeat run",
                       "context": ["line": "Claude Opus 5.5 · about 22% of context used", "use": "about 22% of context used", "detail": "About 44K of 200K tokens in the latest model call (peak about 31%). An estimate from the token counts the provider reported for the latest model call.", "model": "Claude Opus 5.5", "percent": 22, "peak_percent": 31, "tokens": 44000, "window": 200000], "subagents": [
                ["id": "h1", "title": "Check the other payment tests", "status": "done", "goal": "Look for the same fixed wait in the other payment specs",
                 "started_at": ago(5), "ended_at": ago(3.5), "summary": "Two more specs wait 500 ms for the mock: refund.spec.ts and saved-card.spec.ts."],
                ["id": "h2", "title": "Find when the wait was added", "status": "running", "started_at": ago(4), "now": "Reading git blame for checkout.spec.ts"]]]])

        add("s_deploy", "Deploy the pricing page to staging", "discord", "Discord · #launch", ago(31),
            "Wants to run: rsync -az dist/ staging:/srv/site", state: "needs_you", entries: [
                user("m1", ago(48), "Ship the new pricing page to staging when the build is green."),
                tools("t1", [call(1, "run", "Built the site", "terminal", ago(40), 64, args: "npm run build")]),
                bot("m2", ago(31), "The build passed. Uploading needs your OK because it overwrites the staging site."),
                ["kind": "note", "id": "m3", "text": "Continued in Speakeasy · not posted to Discord"],
                user("sk0001-m4", ago(30), "Go ahead and upload it."),
            ], jobs: [["run_id": "run_0011", "status": "waiting_for_approval", "title": "Upload the pricing page",
                       "detail": "Run rsync -az --delete dist/ staging:/srv/site"]],
            approvals: [["run_id": "run_0011", "description": "Run rsync -az --delete dist/ staging:/srv/site",
                         "choices": ["once", "session", "deny"]]])

        add("s_pricing", "Pricing vs. three competitors", "telegram", "Telegram", ago(95), "Which plan should the comparison put us against?",
            state: "needs_you", entries: [
                user("m1", ago(130), "Compare our pricing with Northwind, Contoso and Fabrikam."),
                tools("t1", [call(1, "browse", "Read northwind.example/pricing", "web_extract", ago(120), 4.2),
                             call(2, "browse", "Read contoso.example/plans", "web_extract", ago(115), 5),
                             call(3, "browse", "Read fabrikam.example/pricing", "web_extract", ago(110), 3.1)]),
                bot("m2", ago(96), "Northwind and Contoso charge per seat; Fabrikam is flat per workspace. We're the only one without an annual discount."),
            ], jobs: [["run_id": "run_0009", "status": "completed", "title": "Pricing vs. three competitors",
                       "context": ["line": "GPT-6.1 Mini · about 6% of context used · 23 s", "use": "about 6% of context used", "took": "23 s", "detail": "About 24K of 400K tokens in the latest model call. An estimate from the token counts the provider reported for the latest model call.", "model": "GPT-6.1 Mini", "percent": 6, "tokens": 24000, "window": 400000],
                       "result": ["views": [["kind": "question", "question": "Which plan should the comparison put us against?",
                                             "options": [["text": "Their Team plans"], ["text": "Their Business plans"]]]]]]])

        add("s_dash", "Speed up the sales dashboard", "speakeasy", "Speakeasy", ago(232),
            "Loads in 0.8 s now, down from 6. One query was scanning every order.", entries: [
                user("m1", ago(260), "The sales dashboard is slow. Fix it."),
                tools("t1", [call(1, "run", "Timed every query on the page", "terminal", ago(255), 42),
                             call(2, "edit", "Added an index on orders.created_at", "patch", ago(245), 0.8),
                             call(3, "run", "Checked the load time", "terminal", ago(236), 38, result: "p50 0.8 s (was 6.1 s)")]),
                bot("m2", ago(232), "The dashboard loads in 0.8 seconds now, down from 6. One query was scanning every order; it has an index on created_at now.\n\n```sql\nCREATE INDEX orders_created_at_idx ON orders (created_at);\n```"),
            ], jobs: [["run_id": "run_0090", "status": "completed", "title": "Sped up the sales dashboard",
                          "started_at": ago(260), "finished_at": ago(232)]], pinned: true)
        // Two open questions from separate tasks, and an older one the chat moved on from.
        func asking(_ run: String, _ title: String, _ q: String, _ options: [[String: Any]], at: Double,
                    open: Bool, closed: String? = nil) -> [String: Any] {
            var o: [String: Any] = ["run_id": run, "status": "completed", "title": title, "question_open": open,
                                    "started_at": at - 60, "finished_at": at,
                                    "result": ["views": [["kind": "question", "question": q, "recommended": 0, "options": options]]]]
            if let closed { o["question_closed"] = closed; o["set_aside"] = true }
            return o
        }
        add("s_porch", "Porch makeover", "discord", "Discord · #house", ago(4), "Paint the shed green or grey?",
            state: "needs_you", entries: [
                user("m1", ago(95), "Should the porch light be on a timer or a motion sensor?"),
                bot("m2", ago(92), "Want me to set up the timer now, or wait until the new bulb arrives?"),
                user("m3", ago(60), "Never mind the light. Let's plan the shed and the cedar order instead."),
                bot("m4", ago(8), "Two things need your call before I go on: the shed color and whether to order the boards now."),
            ], jobs: [
                asking("run_shed", "Shed paint color", "Paint the shed green or grey?",
                       [["text": "Sage green", "detail": "Matches the fence trim"], ["text": "Slate grey", "detail": "Hides dirt better"]],
                       at: ago(7), open: true),
                asking("run_cedar", "Cedar board order", "Order the 10 cedar boards now, or wait for the sale on the 15th?",
                       [["text": "Order now", "detail": "$238, arrives Tuesday"], ["text": "Wait for the sale", "detail": "About $200, arrives the 18th"]],
                       at: ago(6), open: true),
                asking("run_light", "Porch light timer", "Set up the timer now, or wait for the new bulb?",
                       [["text": "Set it up now"], ["text": "Wait for the bulb"]], at: ago(91), open: false, closed: "moved_on"),
            ])
        // A long session where several tasks finished along the way (two of them asked twice) and one still runs.
        func job(_ run: String, _ title: String, _ status: String, _ start: Double, _ end: Double? = nil,
                 now: String? = nil) -> [String: Any] {
            var o: [String: Any] = ["run_id": run, "status": status, "title": title, "started_at": start]
            if let end { o["finished_at"] = end }
            if let now { o["short_status"] = now }
            return o
        }
        add("s_long", "Polish the notes app", "speakeasy", "Speakeasy", ago(2), "Running the layout tests after the sidebar change",
            state: "running", entries: [
                user("m1", ago(1630), "Bold text in the chat bubbles shows the asterisks. Fix it."),
                tools("t1", [call(1, "edit", "Edited the message renderer", "patch", ago(1628), 1.2)]),
                bot("m2", ago(1620), "Fixed: bold now renders in every bubble, and the asterisks are gone."),
                user("m3", ago(1590), "A stopped task can't be dismissed. Make it dismissible."),
                tools("t2", [call(2, "edit", "Edited the task list", "patch", ago(1588), 0.9)]),
                bot("m4", ago(1580), "Stopped tasks now have a Dismiss button and swipe away like finished ones."),
                user("m5", ago(120), "The bold fix didn't reach the notifications. Fix that too."),
                bot("m6", ago(110), "Notifications use the same renderer now, so bold shows there as well."),
                user("m7", ago(80), "Make the export button bigger on iPad."),
                bot("m8", ago(72), "The export button is 44 points tall on iPad now, matching the other toolbar buttons."),
                user("m9", ago(40), "Dismissing still doesn't stick after a relaunch."),
                bot("m10", ago(31), "It sticks now: a dismissed task is remembered across relaunches."),
                user("m11", ago(10), "Last one: move the sidebar toggle to the left and rerun the layout tests."),
                tools("t3", [call(3, "edit", "Moved the sidebar toggle", "patch", ago(8), 0.7),
                             call(4, "run", "Running the layout tests", "terminal", ago(6), nil)]),
            ], jobs: [
                job("run_0106", "Move the sidebar toggle left", "running", ago(10), now: "Running the layout tests after the sidebar change"),
                job("run_0105", "Make stopped task dismissible in the app", "completed", ago(40), ago(31)),
                job("run_0104", "Bigger export button on iPad", "completed", ago(80), ago(72)),
                job("run_0103", "Fix asterisk bold in chat bubbles", "completed", ago(120), ago(110)),
                job("run_0102", "Make stopped task dismissible in the app", "completed", ago(1590), ago(1580)),
                job("run_0101", "Fix asterisk bold in chat bubbles", "completed", ago(1630), ago(1620)),
                job("run_0100", "Rename the settings tabs", "completed", ago(1840), ago(1820)),
            ], more: true)
        add("s_tickets", "This week's support tickets", "cli", "Terminal", daysAgo(1, 17, 52),
            "Half the billing tickets are the same card-declined message.", entries: [
                user("m1", daysAgo(1, 17, 40), "Summarize this week's support tickets by cause."),
                bot("m2", daysAgo(1, 17, 52), "77 tickets. Billing (40): half are the same card-declined message. Login (22): mostly the new SSO redirect. Exports (15): CSV timeouts on big accounts."),
            ], unread: true)
        add("s_onboard", "Onboarding email sequence", "speakeasy", "Speakeasy", daysAgo(1, 11, 40), "Drafted all five; email 3 invites the team.",
            entries: [user("m1", daysAgo(1, 11, 5), "Write the onboarding email sequence."),
                      bot("m2", daysAgo(1, 11, 40), "Drafted all five; email 3 invites the team. They're in onboarding/.")])
        add("s_offsite", "Saturday offsite weather", "telegram", "Telegram", daysAgo(3, 9, 1), "Dry until 3 PM, then showers.",
            entries: [user("m1", daysAgo(3, 9), "Will it rain for Saturday's team offsite?"),
                      bot("m2", daysAgo(3, 9, 1), "Dry until 3 PM, then showers. Plan the hike for the morning.")])
        add("s_invoice", "Chase the March invoices", "discord", "Discord · #finance", daysAgo(4, 15, 10), "Couldn't log in to the billing portal.",
            entries: [user("m1", daysAgo(4, 15), "Find which March invoices are still unpaid."),
                      bot("m2", daysAgo(4, 15, 10), "Couldn't log in to the billing portal: it wants a new 2FA code.")])
        add("s_gift", "Pick the shop for the gift", "speakeasy", "Speakeasy", daysAgo(6, 19, 9), "Lark Home: 60-day returns, free label.")
        add("s_rename", "Rename the staging buckets", "cli", "Terminal", daysAgo(9, 14, 20), "Renamed 4 buckets; updated the deploy config.")
        add("s_reading", "Reading list for the team", "speakeasy", "Speakeasy", daysAgo(12, 10, 4), "Six picks on pricing strategy.")
        convs["s_flaky"]?.usage = .demo
        convs["s_flaky"]?.context = .demo
        convs["s_dash"]?.context = .demoDone
        convs["s_pricing"]?.context = .demoFast
        convs["s_dash"]?.usage = .demo
        convs["s_deploy"]?.usage = .demoIncluded
        convs["s_pricing"]?.usage = .demoIncluded
        return (sessions, convs)
    }
}

// MARK: - Screenshots (`--history-shots <dir>`)

@MainActor
enum HistoryShots {
    private static var controller: HistoryWindowController?

    static func run(dir: String) {
        FilmFrames.stillMotion = true
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let c = HistoryWindowController()
        controller = c
        let backend = DemoHistoryBackend()
        c.store.backend = backend
        c.store.recorder = SilentRecorder()
        c.store.scheduleDictation.recorder = SilentRecorder()
        c.store.schedules.backend = DemoScheduleBackend()
        c.store.schedulesSupported = true
        c.store.board.backend = DemoBoardBackend(links: DemoBoardBackend.macLinks)
        c.store.boardsSupported = true
        c.store.files.backend = DemoAgentFilesBackend()
        c.store.filesSupported = true
        if let look = ProcessInfo.processInfo.environment["HISTORY_APPEARANCE"] {
            NSApp.appearance = NSAppearance(named: look == "light" ? .aqua : .darkAqua)
        }
        c.show()
        // HISTORY_WIDTH (e.g. 720, the window's minimum) shoots every page at a narrower window.
        let width = ProcessInfo.processInfo.environment["HISTORY_WIDTH"].flatMap(Double.init) ?? 1040
        c.window?.setContentSize(NSSize(width: max(720, width), height: 720))
        c.window?.center()
        let only = ProcessInfo.processInfo.environment["HISTORY_SHOT"]
        if let look = ProcessInfo.processInfo.environment["HISTORY_APPEARANCE"] {
            NSApp.appearance = NSAppearance(named: look == "light" ? .aqua : .darkAqua)
        }
        Task { @MainActor in
            let store = c.store
            @MainActor func shot(_ name: String, _ setup: @MainActor () async -> Void) async {
                if let only, only != name { return }
                await setup()
                try? await Task.sleep(nanoseconds: 900_000_000)
                do { try snapshot(c, to: "\(dir)/\(name).png"); print("history shot \(name)") }
                catch { print("history shot FAILED \(name): \(error)") }
            }
            try? await Task.sleep(nanoseconds: 600_000_000)
            await shot("1-session-tool-calls") {
                store.open("s_flaky")
                try? await Task.sleep(nanoseconds: 300_000_000)
                store.openSections = ["s_flaky|m4", "s_flaky|m4|b2", "s_flaky|call|m1"]
            }
            await shot("1b-task-live-line") {
                // A running task opened from the panel: its card at the end, with the live line and So far.
                store.openSections = []
                store.open("s_flaky")
            }
            await shot("2-approval") { store.openSections = []; store.open("s_deploy") }
            await shot("3-question-search") {
                store.searchNow("annual")
                store.open("s_pricing")
            }
            await shot("4-recording") {
                store.searchNow("")
                store.setSource(nil)
                store.open("s_flaky")
                var v = MacVoiceMessage()
                v.handle(.micTapped(now: Date())); v.handle(.permissions(granted: true, onDevice: true)); v.handle(.cancel)
                v.handle(.micTapped(now: Date() - 7))   // permission remembered: recording, 7 s in
                v.handle(.partial("Also check the refund spec, it has the same five hundred millisecond wait"))
                store.preview(voice: v, level: 0.7)
            }
            await shot("5-voice-review") {
                var v = MacVoiceMessage()
                v.handle(.micTapped(now: Date())); v.handle(.permissions(granted: true, onDevice: true))
                v.handle(.micTapped(now: Date())); v.handle(.final("Also fix the refund spec, it has the same 500 ms wait."))
                store.preview(voice: v, level: 0)
                store.draft = v.flow.fieldText
            }
            await shot("6-new-chat") {
                store.preview(voice: MacVoiceMessage(), level: 0)
                store.drafts = [:]
                store.newChat()
                store.draft = "Draft a reply to Dana about moving the launch to the 14th"
            }
            await shot("7-sent-and-filter") {
                store.drafts = [:]
                store.setSource("discord")
                store.open("s_deploy")
                var box = HistoryOutbox()
                box.add(HistoryMessageRequest(sessionID: "s_deploy", text: "Only upload the pricing folder, not the whole site.", clientID: "c1")!, at: Date())
                box.add(HistoryMessageRequest(sessionID: "s_deploy", text: "And post the staging link here when it's up.", source: .voiceMessage, clientID: "c2")!, at: Date())
                box.mark("c1", .sent)
                box.mark("c2", .failed("Couldn't reach your Hermes."))
                store.preview(outbox: box)
            }
            await shot("14-files") {
                store.setCallLive(false)
                store.openFiles()
                await store.files.reload()
                for f in store.files.files { await store.files.thumbnail(for: f) }
            }
            await shot("15-files-images") {
                store.openFiles()
                await store.files.choose(kind: "image")
                for f in store.files.files { await store.files.thumbnail(for: f) }
            }
            await shot("8-voice-note") {
                // Stop = sent: one voice note Hermes already wrote down, one still on its way, one that failed (Retry).
                store.setSource(nil)
                store.open("s_flaky")
                var box = HistoryOutbox()
                let wave = (0..<32).map { i in 0.25 + 0.6 * abs(sin(Double(i) * 0.55)) }
                for (id, length) in [("8f0c2a1e-0000-4000-8000-000000000001", 9.0),
                                     ("8f0c2a1e-0000-4000-8000-000000000002", 4.0),
                                     ("8f0c2a1e-0000-4000-8000-000000000003", 6.0)] {
                    let note = VoiceNoteRequest(target: .session("s_flaky"), audio: Data(), clientID: id, duration: length)
                    VoiceClipStore.save(id, audio: Data([0]), meta: .init(duration: length, levels: wave))
                    box.add(voiceNote: note, session: "s_flaky", at: Date())
                }
                box.heard("8f0c2a1e-0000-4000-8000-000000000001",
                          "Also check the refund spec, it has the same five hundred millisecond wait. Then rerun the checkout tests.")
                box.mark("8f0c2a1e-0000-4000-8000-000000000001", .sent)
                box.mark("8f0c2a1e-0000-4000-8000-000000000003", .failed("Couldn't reach your Hermes."))
                store.preview(outbox: box)
            }
            await shot("9-scheduled-list") {
                await store.files.choose(kind: nil)
                store.setCallLive(false)
                store.preview(outbox: HistoryOutbox())
                store.openSchedules()
                await store.schedules.reload()
            }
            await shot("10-scheduled-detail") {
                store.openSchedules("garden")
                await store.schedules.load("garden")
                await store.schedules.runNow("garden")
            }
            await shot("11-scheduled-preview") {
                store.openSchedules()
                await store.schedules.reload()
                store.schedules.startDraft("every weekday at 8 summarize my inbox and flag anything urgent")
                await store.schedules.previewDraft()
            }
            await shot("13-scheduled-new") {
                store.openSchedules()
                store.schedules.startDraft("every morning at 7 ")
            }
            await shot("12-scheduled-paused") {
                store.schedules.startDraft()
                store.openSchedules("reading")
                await store.schedules.load("reading")
            }
            await shot("14-board") {
                store.openBoard()
                await store.board.reload()
                store.board.select("garden-site")
                await store.board.loadPage()
            }
            await shot("15-board-small") {
                store.openBoard()
                await store.board.reload()
                store.board.select("bake-sale")
                await store.board.loadPage()
            }
            await shot("14b-board-narrow") {
                // A small window: the board opens scrolled so the card that needs you is in view, the
                // cut-off edge fades and an arrow shows there's more (SPK-82).
                store.openBoard()
                store.board.select("garden-site")
                await store.board.loadPage()
                c.window?.setContentSize(NSSize(width: 720, height: 480))
            }
            await shot("14c-board-narrow-start") {
                // Scrolled back to the start: the right edge says "1 needs you".
                store.openBoard()
                store.board.select("garden-site")
                await store.board.loadPage()
                c.window?.setContentSize(NSSize(width: 720, height: 480))
                try? await Task.sleep(nanoseconds: 600_000_000)
                NotificationCenter.default.post(name: BoardColumnsRow.scrollToStartForShot, object: nil)
            }
            await shot("14d-board-narrow-end") {
                // Scrolled on to the end: Done lines up at the left margin, a back arrow on the left.
                store.openBoard()
                store.board.select("garden-site")
                await store.board.loadPage()
                c.window?.setContentSize(NSSize(width: 720, height: 480))
                try? await Task.sleep(nanoseconds: 600_000_000)
                NotificationCenter.default.post(name: BoardColumnsRow.scrollToEndForShot, object: nil)
            }
            if only == nil || ["14b-board-narrow", "14c-board-narrow-start", "14d-board-narrow-end"].contains(only!) {
                c.window?.setContentSize(NSSize(width: 1040, height: 720))
            }
            await shot("8-on-call-narrow") {
                store.preview(outbox: HistoryOutbox())
                store.setSource(nil)
                store.setCallLive(true)
                c.window?.setContentSize(NSSize(width: 760, height: 620))
                store.open("s_pricing")
            }
            await shot("9-model-chip") {
                store.setCallLive(false)
                c.window?.setContentSize(NSSize(width: 1040, height: 720))
                store.preview(catalog: ModelCatalog.sample)
                backend.conversations["s_flaky"]?.modelChoice = ModelChoice(model: "anthropic/claude-opus-5-5", reasoning: .high)
                backend.conversations["s_flaky"]?.model = ModelBadge(id: "anthropic/claude-opus-5-5", name: "Claude Opus 5.5",
                                                                    providerName: "Anthropic", reasoning: .high, source: "chat", served: true)
                store.open("s_flaky")
                store.load("s_flaky")
            }
            await shot("10-model-new-chat") {
                store.preview(catalog: ModelCatalog.sample)
                store.newChat()
                store.draft = "Compare the two vendor quotes and pick one"
            }
            if only == nil || only == "11-model-popover" {
                var state = ModelPickerState(catalog: ModelCatalog.sample,
                                             chatChoice: ModelChoice(model: "anthropic/claude-opus-5-5", reasoning: .high))
                state.select(.high)
                snapshotView(ModelPopover(store: store, state: state) {}, size: NSSize(width: 320, height: 430),
                             appearance: c.window?.effectiveAppearance, to: "\(dir)/11-model-popover.png")
                print("history shot 11-model-popover")
            }
            await shot("9-code-block-pinned") {
                store.setCallLive(false)
                c.window?.setContentSize(NSSize(width: 1040, height: 720))
                store.open("s_dash")
            }
            await shot("14-long-session-tasks") {
                c.window?.setContentSize(NSSize(width: 1040, height: 720))
                store.open("s_long")
            }
            let shownBefore = HistoryAutomation.shown
            await shot("16-board-worker") {
                store.setShown(HistoryShown.standard)
                c.window?.setContentSize(NSSize(width: 1040, height: 720))
                store.open("s_board")
            }
            await shot("17-board-worker-instructions") {
                store.openSections = ["s_board_done|m1|instructions"]
                store.open("s_board_done")
            }
            await shot("18-automated-filter") {
                store.openSections = []
                store.open("s_flaky")
            }
            // The menu open: an NSMenu is its own window, so draw the window and the menu together.
            if only == nil || only == "19-automated-filter-menu" {
                try? await Task.sleep(nanoseconds: 400_000_000)
                Self.shootMenu(c, to: "\(dir)/19-automated-filter-menu.png")
            }
            await shot("20-automated-filter-changed") {
                store.setShown(HistoryShown(kanban: false, scheduled: true, helpers: true))
                try? await Task.sleep(nanoseconds: 300_000_000)
                store.open("s_helper")
            }
            store.setShown(HistoryShown.standard)
            await shot("19-two-questions") { store.open("s_porch") }
            await shot("20-question-reply") {
                store.open("s_porch")
                try? await Task.sleep(nanoseconds: 400_000_000)
                if let c = store.conversations["s_porch"], let w = store.thread(c).waiting.last { store.reply(to: w) }
                store.draft = "Order six now and the rest on sale day"
            }
            await shot("21-question-dismissed") {
                store.cancelReply(); store.drafts = [:]
                store.open("s_porch")
                try? await Task.sleep(nanoseconds: 400_000_000)
                if let c = store.conversations["s_porch"], let w = store.thread(c).waiting.first { store.dismiss(w) }
            }
            await shot("12-agent-new-chat") {
                store.preview(catalog: ModelCatalog.sample)
                store.preview(roster: AgentRoster.sample)
                store.newChat()
                store.preview(roster: AgentRoster.sample)
                store.draft = "Summarize this week's papers on soil moisture sensors"
            }
            await shot("13-agent-picked") {
                store.preview(catalog: ModelCatalog.sample)
                store.preview(roster: AgentRoster.sample)
                if store.target != .newChat { store.newChat(); store.preview(roster: AgentRoster.sample) }
                store.pickAgent("research-desk")
                store.draft = "Summarize this week's papers on soil moisture sensors"
            }
            if only == nil || only == "14-agent-popover" {
                snapshotView(AgentPopover(roster: AgentRoster.sample, selected: "research-desk") { _ in },
                             size: NSSize(width: 300, height: 290),
                             appearance: c.window?.effectiveAppearance, to: "\(dir)/14-agent-popover.png")
                print("history shot 14-agent-popover")
            }
            await shot("15-agent-session") {
                backend.conversations["s_reading"]?.agent = AgentRoster.sample.agents[1]
                store.open("s_reading")
                store.load("s_reading")
            }
            await shot("16-usage") {
                store.open("s_dash")
                try? await Task.sleep(nanoseconds: 300_000_000)
                store.openSections = ["s_dash|usage"]
            }
            await shot("16b-usage-plan") { store.openSections = ["s_pricing|usage"]; store.open("s_pricing") }
            await shot("17-context") { store.openSections = []; store.open("s_flaky") }
            await shot("17b-context-done") { store.open("s_pricing") }
            await shot("14-slash-skills") {
                store.skillsSupported = true
                try? await Task.sleep(nanoseconds: 200_000_000)
                store.open("s_flaky")
                store.draft = "/"
            }
            await shot("15-slash-filtered") {
                store.skillsSupported = true
                try? await Task.sleep(nanoseconds: 200_000_000)
                store.newChat()
                store.draft = "/s"
                store.slash.move(1)
            }
            await shot("16-slash-picked") {
                store.skillsSupported = true
                try? await Task.sleep(nanoseconds: 200_000_000)
                store.drafts = [:]
                store.open("s_flaky")
                store.draft = "/soup-scaler for twelve people, in cups"
            }
            HistoryAutomation.shown = shownBefore
            // Steer or queue: nothing above the field while typing on a running task, and the
            // pop-up's two choices at Send (drawn on their own: popovers are separate windows).
            await shot("22-running-typing-no-row") {
                store.drafts = [:]
                backend.conversations["s_flaky"]?.messageModes = true
                store.open("s_flaky")
                try? await Task.sleep(nanoseconds: 400_000_000)
                store.draft = "Also check the refund spec"
            }
            if only == nil || only == "23-send-pop-up" {
                snapshotView(HistorySendModeChoices(store: store), size: NSSize(width: 272, height: 128),
                             appearance: c.window?.effectiveAppearance, to: "\(dir)/23-send-pop-up.png")
                print("history shot 23-send-pop-up")
            }
            if only == nil || only == "24-send-check" {
                // Smoke check of the Send flow on a running task: Send asks, a pick sends with it.
                backend.conversations["s_flaky"]?.messageModes = true
                store.open("s_flaky")
                try? await Task.sleep(nanoseconds: 400_000_000)
                store.draft = "Also check the refund spec"
                let before = backend.sent.count
                store.send()
                let asked = store.choosingSendMode && backend.sent.count == before && store.draft == "Also check the refund spec"
                store.choosingSendMode = false   // a click outside
                let kept = store.draft == "Also check the refund spec" && backend.sent.count == before
                store.send(); store.send(picked: .queue)
                try? await Task.sleep(nanoseconds: 400_000_000)
                let queued = backend.sent.last?.mode == .queue && store.draft.isEmpty && !store.choosingSendMode
                store.draft = "Then a dimmer"; store.send(); store.send(picked: .steer)
                try? await Task.sleep(nanoseconds: 400_000_000)
                let steered = backend.sent.last?.mode == .steer
                print("history send check asked=\(asked) cancelKeeps=\(kept) queued=\(queued) steered=\(steered)")
            }
            exit(0)
        }
    }

    /// Click the filter icon's menu button and, while its menu is open, draw the window with the menu
    /// over it into one image (a menu is its own window, which the window snapshot can't see); then
    /// close the menu. Logs to stderr (unbuffered) so a stuck run still says how far it got.
    static func shootMenu(_ c: HistoryWindowController, to path: String) {
        func log(_ s: String) { FileHandle.standardError.write(Data(("history shot " + s + "\n").utf8)) }
        guard let root = c.window?.contentView else { return }
        func popUps(_ v: NSView) -> [NSView] {
            (v is NSPopUpButton || String(describing: type(of: v)).contains("PopUp") ? [v] : []) + v.subviews.flatMap(popUps)
        }
        // SwiftUI's borderless menus are pop-up buttons; the filter icon is the one at the far end
        // of the sidebar's filter row (the sources menu is at its start).
        let candidates = popUps(root)
        for p in candidates { log("candidate \(type(of: p)) \(p.convert(p.bounds, to: nil))") }
        guard let target = candidates.max(by: { $0.convert($0.bounds, to: nil).midX < $1.convert($1.bounds, to: nil).midX }) else {
            log("FAILED 19-automated-filter-menu: no button"); return
        }
        let windowsBefore = Set(NSApp.windows.map(\.windowNumber))
        let menu = (target as? NSPopUpButton)?.menu
        let timer = Timer(timeInterval: 0.8, repeats: false) { _ in
            MainActor.assumeIsolated {
                defer { menu?.cancelTracking() }
                guard let window = c.window, let view = window.contentView,
                      let base = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
                view.cacheDisplay(in: view.bounds, to: base)
                let out = NSImage(size: view.bounds.size)
                out.lockFocus()
                base.draw(in: NSRect(origin: .zero, size: view.bounds.size))
                for w in NSApp.windows where !windowsBefore.contains(w.windowNumber) && w.isVisible {
                    guard let mv = w.contentView?.superview ?? w.contentView,
                          let rep = mv.bitmapImageRepForCachingDisplay(in: mv.bounds) else { continue }
                    mv.cacheDisplay(in: mv.bounds, to: rep)
                    let f = window.convertFromScreen(w.frame)
                    NSGraphicsContext.saveGraphicsState()
                    NSBezierPath(roundedRect: f.insetBy(dx: 1, dy: 1), xRadius: 9, yRadius: 9).addClip()   // the menu's own corners
                    rep.draw(in: f)
                    NSGraphicsContext.restoreGraphicsState()
                    log("menu window \(type(of: w)) \(w.frame)")
                }
                out.unlockFocus()
                if let tiff = out.tiffRepresentation, let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:]) {
                    try? png.write(to: URL(fileURLWithPath: path))
                    log("19-automated-filter-menu")
                }
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        RunLoop.main.add(timer, forMode: .eventTracking)
        log("clicking \(type(of: target))")
        if let button = target as? NSButton { button.performClick(nil) } else { _ = target.accessibilityPerformPress() }
        log("menu closed")
    }

    /// A popover's content on its own (popovers are separate windows the main snapshot can't see).
    static func snapshotView<V: View>(_ view: V, size: NSSize, appearance: NSAppearance?, to path: String) {
        let host = NSHostingView(rootView: view.background(.regularMaterial))
        host.frame = NSRect(origin: .zero, size: size)
        let window = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        window.appearance = appearance
        window.contentView = host
        window.orderFrontRegardless()
        host.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.4))
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return }
        host.cacheDisplay(in: host.bounds, to: rep)
        try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
        window.orderOut(nil)
    }

    static func snapshot(_ c: HistoryWindowController, to path: String) throws {
        guard let view = c.window?.contentView else { throw CocoaError(.fileWriteUnknown) }
        view.layoutSubtreeIfNeeded()
        let bounds = view.bounds
        guard let rep = view.bitmapImageRepForCachingDisplay(in: bounds) else { throw CocoaError(.fileWriteUnknown) }
        view.cacheDisplay(in: bounds, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]) else { throw CocoaError(.fileWriteUnknown) }
        try png.write(to: URL(fileURLWithPath: path))
    }
}

/// Screenshots: no mic.
@MainActor
final class SilentRecorder: VoiceMessageRecording {
    func authorize(_ done: @escaping @MainActor (Bool, Bool) -> Void) { done(false, true) }
    func start(partial: @escaping @MainActor (String) -> Void, level: @escaping @MainActor (Float) -> Void,
               finished: @escaping @MainActor (String?, String?) -> Void) -> Bool { false }
    func stop() {}
    func cancel() {}
}
