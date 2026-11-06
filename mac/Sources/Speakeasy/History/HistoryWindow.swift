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
        else if query.hideAutomated { all = all.filter { !$0.automated } }
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
        let automated = sessions.filter(\.automated).count
        return HistoryPage(sessions: slice, nextCursor: next, filters: HistoryList.defaultFilters,
                           sources: HistoryList.defaultFilters.compactMap { f in
                               guard !(query.hideAutomated && ["kanban", "scheduled"].contains(f.id)) else { return nil }
                               return counts[f.id].map { HistorySourceCount(source: f.id, count: $0) }
                           },
                           automated: HistoryAutomated(count: automated, hidden: query.hideAutomated))
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
    func setSessionModel(_ request: SessionModelRequest) async throws {
        sessionModels.append(request)
        conversations[request.sessionID]?.modelChoice = request.choice.isEmpty ? nil : request.choice
    }
    func setDefaultModel(_ defaults: JobModelDefaults) async throws {
        models.defaultChoice = ModelChoice(model: defaults.model, reasoning: defaults.reasoning)
    }

    func startChat(text: String, clientID: String, choice: ModelChoice?) async throws -> String? {
        let id = "s_new_\(sessions.count)", run = "run_new_\(sessions.count)"
        let s = HistorySession(id: id, title: text, source: "speakeasy", sourceLabel: "Speakeasy", lastActivity: Date(),
                               preview: text, state: .running, runIDs: [run])
        sessions.insert(s, at: 0)
        conversations[id] = HistoryConversation(id: id, title: text, source: "speakeasy", sourceLabel: "Speakeasy", state: .running,
                                                sendVia: .task, entries: [HistoryEntry(id: "m0", kind: .user, at: Date(), text: text)])
        return run
    }

    func answerApproval(sessionID: String, runID: String, choice: String) async throws { answers.append((sessionID, runID, choice)) }
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
            if source == "kanban" || source == "cron" { row["automated"] = true }
            sessions.append(HistorySession(json: row)!)
            var full = row
            full["entries"] = entries; full["jobs"] = jobs; full["approvals"] = approvals; full["has_more"] = more
            full["send_via"] = source == "speakeasy" ? "task" : "linked"
            full.merge(extra) { $1 }
            convs[id] = HistoryConversation(json: ["session": full])
        }

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
                       "short_status": "Rerunning the checkout test: 31 of 50", "step_index": 3, "step_count": 4, "subagents": [
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
                       "result": ["views": [["kind": "question", "question": "Which plan should the comparison put us against?",
                                             "options": [["text": "Their Team plans"], ["text": "Their Business plans"]]]]]]])

        add("s_dash", "Speed up the sales dashboard", "speakeasy", "Speakeasy", ago(232),
            "Loads in 0.8 s now, down from 6. One query was scanning every order.", entries: [
                user("m1", ago(260), "The sales dashboard is slow. Fix it."),
                tools("t1", [call(1, "run", "Timed every query on the page", "terminal", ago(255), 42),
                             call(2, "edit", "Added an index on orders.created_at", "patch", ago(245), 0.8),
                             call(3, "run", "Checked the load time", "terminal", ago(236), 38, result: "p50 0.8 s (was 6.1 s)")]),
                bot("m2", ago(232), "The dashboard loads in 0.8 seconds now, down from 6. One query was scanning every order; it has an index on created_at now.\n\n```sql\nCREATE INDEX orders_created_at_idx ON orders (created_at);\n```"),
            ], pinned: true)
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
                user("m1", ago(190), "Bold text in the chat bubbles shows the asterisks. Fix it."),
                tools("t1", [call(1, "edit", "Edited the message renderer", "patch", ago(188), 1.2)]),
                bot("m2", ago(180), "Fixed: bold now renders in every bubble, and the asterisks are gone."),
                user("m3", ago(150), "A stopped task can't be dismissed. Make it dismissible."),
                tools("t2", [call(2, "edit", "Edited the task list", "patch", ago(148), 0.9)]),
                bot("m4", ago(140), "Stopped tasks now have a Dismiss button and swipe away like finished ones."),
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
                job("run_0102", "Make stopped task dismissible in the app", "completed", ago(150), ago(140)),
                job("run_0101", "Fix asterisk bold in chat bubbles", "completed", ago(190), ago(180)),
                job("run_0100", "Rename the settings tabs", "completed", ago(400), ago(380)),
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
        if let look = ProcessInfo.processInfo.environment["HISTORY_APPEARANCE"] {
            NSApp.appearance = NSAppearance(named: look == "light" ? .aqua : .darkAqua)
        }
        c.show()
        c.window?.setContentSize(NSSize(width: 1040, height: 720))
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
            await shot("9-scheduled-list") {
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
            await shot("16-board-worker") {
                store.setHideAutomated(false)
                c.window?.setContentSize(NSSize(width: 1040, height: 720))
                store.open("s_board")
            }
            await shot("17-board-worker-instructions") {
                store.openSections = ["s_board_done|m1|instructions"]
                store.open("s_board_done")
            }
            await shot("18-automated-hidden") {
                store.setHideAutomated(true)
                try? await Task.sleep(nanoseconds: 300_000_000)
                store.open("s_flaky")
            }
            store.setHideAutomated(false)
            exit(0)
        }
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
