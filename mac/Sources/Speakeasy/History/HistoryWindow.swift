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
        w.appearance = NSAppearance(named: .darkAqua)
        w.backgroundColor = NSColor(calibratedRed: 0.118, green: 0.110, blue: 0.102, alpha: 1)
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

// MARK: - Demo (screenshots; invented, work-first data)

@MainActor
final class DemoHistoryBackend: HistoryBackend {
    var sessions: [HistorySession]
    var details: [String: HistoryDetail]
    var sent: [HistoryMessageRequest] = []

    init(now: Date = Date()) {
        (sessions, details) = DemoHistory.make(now: now)
    }

    func page(_ query: HistoryQuery) async throws -> HistoryPage {
        var all = sessions
        if let source = query.source { all = all.filter { $0.source == source } }
        if let q = query.q?.lowercased() {
            all = all.compactMap { s in
                if s.title.lowercased().contains(q) { return s }
                guard let hit = details[s.id]?.messages.first(where: { $0.text.lowercased().contains(q) }) else { return nil }
                var m = s; m.snippet = DemoHistory.snippet(hit.text, around: q); return m
            }
        }
        let start = Int(query.cursor ?? "0") ?? 0
        let slice = Array(all.dropFirst(start).prefix(query.limit))
        let next = start + slice.count < all.count ? String(start + slice.count) : nil
        let counts = Dictionary(grouping: sessions, by: { $0.source ?? "" }).mapValues(\.count)
        let sources = ["speakeasy", "discord", "telegram", "cli", "cron"].map { HistorySource(id: $0, count: counts[$0]) }
        return HistoryPage(sessions: slice, nextCursor: next, sources: sources)
    }

    func detail(_ sessionID: String) async throws -> HistoryDetail {
        guard let d = details[sessionID] else { throw ServerClient.HTTPError(status: 404, message: "I couldn't find that session.") }
        return d
    }

    func send(_ request: HistoryMessageRequest) async throws -> HistoryMessageReply {
        sent.append(request)
        return HistoryMessageReply(sessionID: request.sessionID, runID: nil, deliveredAs: .steer)
    }

    var answers: [HistorySendAnswer] = []

    func startChat(text: String, clientID: String) async throws -> String? {
        let id = "s_new_\(sessions.count)", run = "run_new_\(sessions.count)"
        let s = HistorySession(id: id, title: text, source: "speakeasy", preview: text, startedAt: Date(), updatedAt: Date(),
                               status: .running, runID: run)
        sessions.insert(s, at: 0)
        details[id] = HistoryDetail(session: s, messages: [HistoryMessage(id: "m0", role: .user, text: text, at: Date(), source: "typed")])
        return run
    }

    func answer(_ answer: HistorySendAnswer) async throws { answers.append(answer) }
}

enum DemoHistory {
    static func snippet(_ text: String, around q: String) -> String {
        let flat = HistorySession.oneLine(text)
        guard let r = flat.range(of: q, options: .caseInsensitive) else { return String(flat.prefix(90)) }
        let start = flat.index(r.lowerBound, offsetBy: -30, limitedBy: flat.startIndex) ?? flat.startIndex
        return (start == flat.startIndex ? "" : "…") + String(flat[start...].prefix(100))
    }

    static func call(_ id: String, _ kind: JobActivityItem.Kind, _ text: String, _ tool: String, at: Date, took: Double?,
                     args: String? = nil, result: String? = nil, failed: Bool = false) -> JobActivityItem {
        JobActivityItem(id: id, at: at, kind: kind, text: text, status: took == nil ? .running : failed ? .failed : .done,
                        tool: tool, startedAt: at, endedAt: took.map { at + $0 }, args: args, result: result)
    }

    static func make(now: Date) -> ([HistorySession], [String: HistoryDetail]) {
        let cal = Calendar.current
        func today(_ minutesAgo: Double) -> Date { now - minutesAgo * 60 }
        func daysAgo(_ d: Int, _ hour: Int, _ minute: Int = 0) -> Date {
            let day = cal.date(byAdding: .day, value: -d, to: now)!
            return cal.date(bySettingHour: hour, minute: minute, second: 0, of: day)!
        }
        var sessions: [HistorySession] = []
        var details: [String: HistoryDetail] = [:]
        func add(_ s: HistorySession, _ messages: [HistoryMessage] = [], helpers: [JobSubagent] = [],
                 approvals: [HistoryApproval] = [], questions: [HistoryQuestion] = []) {
            sessions.append(s)
            details[s.id] = HistoryDetail(session: s, messages: messages, helpers: helpers, approvals: approvals, questions: questions)
        }

        // 1. Running, with tool calls and helpers.
        let t0 = today(9)
        add(HistorySession(id: "s_flaky", title: "Fix the flaky checkout test", source: "speakeasy",
                           preview: "Rerunning the checkout test 50 times", startedAt: t0, updatedAt: today(0.5), status: .running, messageCount: 4),
            [HistoryMessage(id: "m1", role: .user, text: "The checkout test keeps failing on CI. Find out why and fix it.", at: t0, source: "voice"),
             HistoryMessage(id: "m2", role: .assistant,
                            text: "It's a race: the test clicks **Pay** before the payment mock is listening, so it fails about 1 run in 6. A fixed 500 ms wait was standing in for “mock ready”.",
                            at: today(6), toolCalls: [
                                call("a1", .search, "Searched the files for “checkout”", "search_files", at: t0 + 10, took: 1.8, args: "checkout",
                                     result: "3 matches\ncheckout.spec.ts:12  test('checkout', …)\npayment-mock.ts:1  // payment mock"),
                                call("a2", .read, "Read checkout.spec.ts", "read_file", at: t0 + 14, took: 0.6, args: "checkout.spec.ts",
                                     result: "41  await page.waitForTimeout(500)\n42  await page.click('#pay')"),
                                call("a3", .run, "Ran the test 12 times", "terminal", at: t0 + 20, took: 61,
                                     args: "npx playwright test checkout.spec.ts --repeat-each=12",
                                     result: "10 passed, 2 failed (1.2m)\n  checkout.spec.ts:43  Timed out waiting for “Order placed”", failed: true),
                                call("a4", .read, "Read payment-mock.ts", "read_file", at: t0 + 90, took: 0.5),
                            ]),
             HistoryMessage(id: "m3", role: .user, text: "Wait on the mock's ready signal instead of the timeout, then run it 50 times.", at: today(5), source: "typed"),
             HistoryMessage(id: "m4", role: .assistant, text: "Changed the test to wait for the mock. Running it 50 times now: 31 passes so far, no failures.",
                            at: today(1), toolCalls: [
                                call("b1", .edit, "Edited payment-mock.ts", "patch", at: today(4.8), took: 0.7, args: "payment-mock.ts",
                                     result: "+  export const mockReady = new Promise(r => server.on('listening', r))"),
                                call("b2", .edit, "Edited checkout.spec.ts", "patch", at: today(4.6), took: 0.6, args: "checkout.spec.ts",
                                     result: "-  await page.waitForTimeout(500)\n+  await mockReady"),
                                call("b3", .run, "Ran the test 50 times", "terminal", at: today(4), took: nil,
                                     args: "npx playwright test checkout.spec.ts --repeat-each=50"),
                            ])],
            helpers: [JobSubagent(id: "h1", title: "Check the other payment tests", status: .done, goal: "Look for the same fixed wait in the other payment specs",
                                  startedAt: today(5), endedAt: today(3.5), summary: "Two more specs wait 500 ms for the mock: refund.spec.ts and saved-card.spec.ts."),
                      JobSubagent(id: "h2", title: "Find when the wait was added", status: .running, startedAt: today(4), now: "Reading git blame for checkout.spec.ts")])

        // 2. Waiting on an approval (Discord).
        add(HistorySession(id: "s_deploy", title: "Deploy the pricing page to staging", source: "discord",
                           preview: "Wants to run: rsync -az dist/ staging:/srv/site", startedAt: today(48), updatedAt: today(31), status: .waiting),
            [HistoryMessage(id: "m1", role: .user, text: "Ship the new pricing page to staging when the build is green.", at: today(48), source: "discord"),
             HistoryMessage(id: "m2", role: .assistant, text: "The build passed. Uploading needs your OK because it overwrites the staging site.",
                            at: today(31), toolCalls: [call("d1", .run, "Built the site", "terminal", at: today(40), took: 64, args: "npm run build")])],
            approvals: [HistoryApproval(id: "ap1", description: "Upload the built site to staging", command: "rsync -az --delete dist/ staging:/srv/site")])

        // 3. A question (Telegram).
        add(HistorySession(id: "s_pricing", title: "Pricing vs. three competitors", source: "telegram",
                           preview: "Which plan should the comparison put us against?", startedAt: today(130), updatedAt: today(95), status: .waiting),
            [HistoryMessage(id: "m1", role: .user, text: "Compare our pricing with Northwind, Contoso and Fabrikam.", at: today(130), source: "telegram"),
             HistoryMessage(id: "m2", role: .assistant, text: "Northwind and Contoso charge per seat; Fabrikam is flat per workspace. We're the only one without an annual discount.",
                            at: today(96), toolCalls: [
                                call("p1", .browse, "Read northwind.example/pricing", "web_extract", at: today(120), took: 4.2),
                                call("p2", .browse, "Read contoso.example/plans", "web_extract", at: today(115), took: 5),
                                call("p3", .browse, "Read fabrikam.example/pricing", "web_extract", at: today(110), took: 3.1)])],
            questions: [HistoryQuestion(id: "q1", question: "Which plan should the comparison put us against?",
                                        options: ["Their Team plans", "Their Business plans"])])

        add(HistorySession(id: "s_dash", title: "Speed up the sales dashboard", source: "speakeasy",
                           preview: "Loads in 0.8 s now, down from 6. One query was scanning every order.", startedAt: today(260), updatedAt: today(232), status: .done),
            [HistoryMessage(id: "m1", role: .user, text: "The sales dashboard is slow. Fix it.", at: today(260), source: "voice"),
             HistoryMessage(id: "m2", role: .assistant, text: "The dashboard loads in 0.8 seconds now, down from 6. One query was scanning every order; it has an index on created_at now.",
                            at: today(232), toolCalls: [
                                call("c1", .run, "Timed every query on the page", "terminal", at: today(255), took: 42, args: "npm run profile -- /sales"),
                                call("c2", .edit, "Added an index on orders.created_at", "patch", at: today(245), took: 0.8),
                                call("c3", .run, "Checked the load time", "terminal", at: today(236), took: 38, result: "p50 0.8 s (was 6.1 s)")])])

        // Yesterday.
        add(HistorySession(id: "s_tickets", title: "This week's support tickets", source: "cli",
                           preview: "Half the billing tickets are the same card-declined message.", startedAt: daysAgo(1, 17, 40), updatedAt: daysAgo(1, 17, 52), status: .done),
            [HistoryMessage(id: "m1", role: .user, text: "Summarize this week's support tickets by cause.", at: daysAgo(1, 17, 40), source: "cli"),
             HistoryMessage(id: "m2", role: .assistant, text: "77 tickets. Billing (40): half are the same card-declined message. Login (22): mostly the new SSO redirect. Exports (15): CSV timeouts on big accounts.",
                            at: daysAgo(1, 17, 52))])
        add(HistorySession(id: "s_brief", title: "Morning brief", source: "cron", preview: "3 meetings, 2 PRs waiting on you, rain after 4 PM.",
                           startedAt: daysAgo(1, 8), updatedAt: daysAgo(1, 8, 1), status: .done),
            [HistoryMessage(id: "m1", role: .assistant, text: "3 meetings, 2 PRs waiting on you, rain after 4 PM.", at: daysAgo(1, 8, 1))])
        add(HistorySession(id: "s_onboard", title: "Onboarding email sequence", source: "speakeasy",
                           preview: "Drafted all five; email 3 invites the team.", startedAt: daysAgo(1, 11, 5), updatedAt: daysAgo(1, 11, 40), status: .done),
            [HistoryMessage(id: "m1", role: .user, text: "Write the onboarding email sequence.", at: daysAgo(1, 11, 5), source: "typed"),
             HistoryMessage(id: "m2", role: .assistant, text: "Drafted all five; email 3 invites the team. They're in onboarding/.", at: daysAgo(1, 11, 40))])

        // Earlier.
        add(HistorySession(id: "s_offsite", title: "Saturday offsite weather", source: "telegram", preview: "Dry until 3 PM, then showers.",
                           startedAt: daysAgo(3, 9), updatedAt: daysAgo(3, 9, 1), status: .done),
            [HistoryMessage(id: "m1", role: .user, text: "Will it rain for Saturday's team offsite?", at: daysAgo(3, 9), source: "telegram"),
             HistoryMessage(id: "m2", role: .assistant, text: "Dry until 3 PM, then showers. Plan the hike for the morning.", at: daysAgo(3, 9, 1))])
        add(HistorySession(id: "s_invoice", title: "Chase the March invoices", source: "discord", preview: "Couldn't log in to the billing portal.",
                           startedAt: daysAgo(4, 15), updatedAt: daysAgo(4, 15, 10), status: .failed),
            [HistoryMessage(id: "m1", role: .user, text: "Find which March invoices are still unpaid.", at: daysAgo(4, 15), source: "discord"),
             HistoryMessage(id: "m2", role: .assistant, text: "Couldn't log in to the billing portal: it wants a new 2FA code.", at: daysAgo(4, 15, 10))])
        add(HistorySession(id: "s_gift", title: "Pick the shop for the gift", source: "speakeasy", preview: "Lark Home: 60-day returns, free label.",
                           startedAt: daysAgo(6, 19), updatedAt: daysAgo(6, 19, 9), status: .done),
            [HistoryMessage(id: "m1", role: .user, text: "Which shop has the best return policy for the lamp?", at: daysAgo(6, 19), source: "voice"),
             HistoryMessage(id: "m2", role: .assistant, text: "Lark Home: 60-day returns with a free label.", at: daysAgo(6, 19, 9))])
        add(HistorySession(id: "s_rename", title: "Rename the staging buckets", source: "cli", preview: "Renamed 4 buckets; updated the deploy config.",
                           startedAt: daysAgo(9, 14), updatedAt: daysAgo(9, 14, 20), status: .done))
        add(HistorySession(id: "s_reading", title: "Reading list for the team", source: "speakeasy", preview: "Six picks on pricing strategy.",
                           startedAt: daysAgo(12, 10), updatedAt: daysAgo(12, 10, 4), status: .done))
        return (sessions, details)
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
        c.show()
        c.window?.setContentSize(NSSize(width: 1040, height: 720))
        c.window?.center()
        let only = ProcessInfo.processInfo.environment["HISTORY_SHOT"]
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
                store.openSections = ["s_flaky|m4", "s_flaky|m4|b2"]
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
                var f = VoiceMessageFlow()
                _ = f.tapMic(); f.permission(granted: true, now: Date() - 7)
                f.heard("Also check the refund spec, it has the same five hundred millisecond wait")
                store.preview(voice: f, level: 0.7)
            }
            await shot("5-voice-review") {
                var f = VoiceMessageFlow()
                _ = f.tapMic(); f.permission(granted: true, now: Date() - 9)
                f.heard("Also fix the refund spec, it has the same 500 ms wait."); f.stop(); f.finished(nil)
                store.preview(voice: f, level: 0)
            }
            await shot("6-new-chat") {
                store.cancelVoice()
                store.newChat()
                store.draft = "Draft a reply to Dana about moving the launch to the 14th"
            }
            await shot("7-sent-and-filter") {
                store.drafts = [:]
                store.setSource("discord")
                store.open("s_deploy")
                var box = HistoryOutbox()
                box.add(HistoryMessageRequest(sessionID: "s_deploy", text: "Only upload the pricing folder, not the whole site.", clientID: "c1"), at: Date())
                box.add(HistoryMessageRequest(sessionID: "s_deploy", text: "And post the staging link here when it's up.", source: .voice, clientID: "c2"), at: Date())
                box.mark("c1", .sent)
                box.mark("c2", .failed("Couldn't reach your Hermes."))
                store.preview(outbox: box)
            }
            await shot("8-on-call-narrow") {
                store.preview(outbox: HistoryOutbox())
                store.setSource(nil)
                store.setCallLive(true)
                c.window?.setContentSize(NSSize(width: 760, height: 620))
                store.open("s_pricing")
            }
            exit(0)
        }
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
    func authorize(_ done: @escaping @MainActor (Bool, String?) -> Void) { done(false, "No microphone in screenshots.") }
    func start(partial: @escaping @MainActor (String) -> Void, level: @escaping @MainActor (Float) -> Void,
               finished: @escaping @MainActor (String?, String?) -> Void) -> Bool { false }
    func stop() {}
    func cancel() {}
}
