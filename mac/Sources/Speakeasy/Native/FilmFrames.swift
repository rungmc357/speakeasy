import AppKit
import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

/// `--film-frames <dir>`: draws the real call panel through a scripted call (invented data) and saves
/// one transparent 2x PNG per beat, for the launch video. Offline: no api, mic or network.
@MainActor
enum FilmFrames {
    private static var retained: NativeVoiceClient?
    /// Website screenshots (`SPEAKEASY_FILM_SET=site`): no keyboard hints under the board.
    static var cleanShots = false

    static func run(config: AppConfig, dir: String) {
        let client = NativeVoiceClient(config: config)
        retained = client
        client.model.loadCardImage = { _, n in FileManager.default.contents(atPath: "\(dir)/picture-\(n).png") }
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        FileHandle.standardError.write("film: start\n".data(using: .utf8)!)
        Task { @MainActor in
            FileHandle.standardError.write("film: task\n".data(using: .utf8)!)
            let set = ProcessInfo.processInfo.environment["SPEAKEASY_FILM_SET"]
            let site = set == "site"
            cleanShots = site || set == "ux"
            let shots: [Shot] = set == "ux" ? uxBeats() : site ? siteBeats().map { Shot($0.0, $0.1, $0.2) } : beats().map { Shot($0.0, $0.1, nil) }
            for shot in shots {
                let (name, state, open) = (shot.name, shot.state, shot.open)
                let m = client.model
                ChartPreview.selection = nil
                m.orbLevel = 0; m.previewProblem = nil; m.pendingPicks = [:]; m.toast = nil; m.openSections = []
                if set == "ux" { for id in ["c", "a", "p", "f", "h", "m", "b"] { m.answers[id] = nil } }
                m.capabilities = set == "ux" ? ServerCapabilities(cardActions: true, jobProgress: true, spokenChoices: true) : nil
                client.showPreview(state, workExpanded: false)
                client.model.onOpenJob(open)
                client.model.boardFocusID = nil
                shot.setup(m)
                try? await Task.sleep(nanoseconds: 900_000_000)
                do { try snapshot(client, to: "\(dir)/\(name).png"); print("film \(name)") }
                catch { print("film FAILED \(name): \(error)") }
            }
            exit(0)
        }
    }

    static func snapshot(_ c: NativeVoiceClient, to path: String) throws {
        let panel = c.panel
        guard let view = panel.window.contentView else { throw CocoaError(.fileWriteUnknown) }
        panel.setNeedsResize()
        view.layoutSubtreeIfNeeded()
        let bounds = view.bounds
        guard let rep = view.bitmapImageRepForCachingDisplay(in: bounds) else { throw CocoaError(.fileWriteUnknown) }
        view.cacheDisplay(in: bounds, to: rep)
        let scale: CGFloat = 2
        guard let out = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(bounds.width * scale),
                                         pixelsHigh: Int(bounds.height * scale), bitsPerSample: 8, samplesPerPixel: 4,
                                         hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                         bytesPerRow: 0, bitsPerPixel: 0) else { throw CocoaError(.fileWriteUnknown) }
        out.size = bounds.size
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: out)
        NSGraphicsContext.current?.imageInterpolation = .high
        let shape = NSBezierPath(roundedRect: bounds, xRadius: Tokens.radius, yRadius: Tokens.radius)
        NSColor(calibratedRed: 0.11, green: 0.12, blue: 0.15, alpha: 0.94).setFill()
        shape.fill()
        shape.addClip()
        rep.draw(in: bounds)
        NSGraphicsContext.restoreGraphicsState()
        guard let png = out.representation(using: .png, properties: [:]) else { throw CocoaError(.fileWriteUnknown) }
        try png.write(to: URL(fileURLWithPath: path))
    }

    // MARK: Script

    struct Shot {
        var name: String; var state: VoiceState; var open: String?
        var setup: @MainActor (VoicePanelModel) -> Void = { _ in }
        init(_ name: String, _ state: VoiceState, _ open: String?, setup: @escaping @MainActor (VoicePanelModel) -> Void = { _ in }) {
            self.name = name; self.state = state; self.open = open; self.setup = setup
        }
    }

    static func card(_ json: [String: Any]) -> ViewCard { ViewCard(json: json, number: 1)! }

    static func task(_ id: String, _ status: String, _ request: String, title: String, short: String?,
                     age: TimeInterval, now: Date, result: WorkResult? = nil,
                     milestones: [String] = []) -> TaskItem {
        var events = [WorkEventItem(kind: "request", text: request, at: now - age - 2)]
        for (i, m) in milestones.enumerated() {
            events.append(WorkEventItem(kind: "milestone", text: m, at: now - age + Double(i)))
        }
        return TaskItem(id: id, info: WorkInfo(runID: "run_\(id)", status: status, updated: now - age, shortStatus: short,
                                               updatedAt: now - 1, statusSource: "authored", events: events,
                                               result: result, title: title))
    }

    static func beats() -> [(String, VoiceState)] {
        let now = Date()
        func base() -> VoiceState {
            var s = VoiceState(); s.now = now; s.connection = .live; s.interactionID = "vi_film"; return s
        }
        let quote = card(["kind": "quote", "asset": "stock", "symbol": "NVDA", "name": "NVIDIA", "price": 187.42,
                          "currency": "USD", "change": 3.86, "change_pct": 2.1,
                          "points": [183.56, 184.1, 183.2, 184.9, 185.6, 185.1, 186.0, 186.9, 186.4, 187.1, 186.8, 187.42],
                          "previous_close": 183.56, "range_label": "Today", "exchange": "NASDAQ"])
        let quoteTask = task("q", "completed", "What's Nvidia at?", title: "NVIDIA price", short: "Done", age: 1, now: now,
                             result: WorkResult(spoken: "Nvidia's at 187.42, up 2.1% today.", full: nil, views: [quote]))
        let home = card(["kind": "home", "title": "Living room", "devices": [
            ["name": "Ceiling lights", "kind": "light", "on": true, "level": 30],
            ["name": "Lamp", "kind": "light", "on": true, "level": 30],
            ["name": "Speaker", "kind": "media", "on": true, "state": "Playing jazz"]]])
        let homeTask = task("h", "completed", "Dim the living room and put on some jazz", title: "Living room", short: "Done",
                            age: 1, now: now, result: WorkResult(spoken: "Done.", full: nil, views: [home]))
        let dinnerReq = "Find us a dinner spot near the office Thursday, for four, and draft an invite to the team"
        let dinnerWorking = task("d", "working", dinnerReq, title: "Thursday dinner for four",
                                 short: "Checking tables for 7 PM", age: 6, now: now,
                                 milestones: ["Pulled places within a 10-minute walk", "Checking tables for 7 PM"])
        let game = card(["kind": "game", "league": "NBA", "status": "final", "detail": "Final", "when": "Last night",
                         "teams": [["name": "Knicks", "abbr": "NYK", "score": "112", "winner": true, "record": "3-1"],
                                   ["name": "Celtics", "abbr": "BOS", "score": "104", "winner": false, "record": "2-2"]]])
        let gameTask = task("g", "completed", "Who won the Knicks game?", title: "Knicks score", short: "Done", age: 1,
                            now: now, result: WorkResult(spoken: "The Knicks beat Boston 112 to 104.", full: nil, views: [game]))
        let question = card(["kind": "question", "question": "Three places have a table at 7. Which one?",
                             "options": ["Hearth & Vine", "Osteria Nove", "Baan Lime"], "recommended": 1, "images": [1, 2, 3]])
        let dinnerAsk = task("d", "working", dinnerReq, title: "Thursday dinner for four", short: "Needs your pick", age: 2,
                             now: now, result: WorkResult(spoken: nil, full: nil, views: [question]))
        let draft = EmailDraft(draftID: "film_draft", sha256: "f11m", from: "you@example.com",
                               to: ["team@example.com"], cc: [], subject: "Dinner Thursday, 7 PM",
                               body: "Hi all,\n\nTable for four at Osteria Nove this Thursday at 7. It's a five-minute walk from the office.\n\nSee you there!")
        var dinnerDraft = task("d", "waiting_for_approval", dinnerReq, title: "Thursday dinner for four",
                               short: "Booked 7 PM · invite drafted", age: 2, now: now)
        dinnerDraft.info.emailDrafts = [draft]

        var out: [(String, VoiceState)] = []
        var s = base()
        s.exchange = Exchange(you: "", assistant: "", replyStarted: false)
        out.append(("01-idle", s))
        s = base(); s.exchange = Exchange(you: "What's Nvidia at?", assistant: "", replyStarted: false); s.lastInputAt = now
        out.append(("02-ask-quote", s))
        s = base(); s.speech = .speaking(lastDelta: now)
        s.exchange = Exchange(you: "What's Nvidia at?", assistant: "Nvidia's at 187.42, up 2.1% today.", replyStarted: true)
        s.tasks = [quoteTask]; s.workInfo = quoteTask.info
        out.append(("03-quote", s))
        s = base(); s.speech = .speaking(lastDelta: now)
        s.exchange = Exchange(you: "Dim the living room and put on some jazz.", assistant: "Done.", replyStarted: true)
        s.tasks = [quoteTask, homeTask]; s.workInfo = homeTask.info
        out.append(("04-home", s))
        s = base(); s.speech = .speaking(lastDelta: now)
        s.exchange = Exchange(you: "Find us a dinner spot near the office Thursday, for four, and draft an invite to the team.",
                              assistant: "On it. I'll let you know what I find.", replyStarted: true)
        s.tasks = [homeTask, dinnerWorking]; s.runID = "run_d"; s.delegationAt = now - 6; s.workInfo = dinnerWorking.info
        out.append(("05-dinner-working", s))
        s = base(); s.speech = .speaking(lastDelta: now)
        s.exchange = Exchange(you: "Who won the Knicks game?", assistant: "The Knicks beat Boston 112 to 104.", replyStarted: true)
        s.tasks = [dinnerWorking, gameTask]; s.runID = "run_d"; s.delegationAt = now - 20; s.workInfo = dinnerWorking.info
        out.append(("06-game", s))
        s = base(); s.speech = .speaking(lastDelta: now)
        s.exchange = Exchange(you: "", assistant: "Three spots at 7. I'd go with Osteria Nove.", replyStarted: true)
        s.tasks = [gameTask, dinnerAsk]; s.runID = "run_d"; s.workInfo = dinnerAsk.info
        out.append(("07-question", s))
        s = base(); s.speech = .speaking(lastDelta: now)
        s.exchange = Exchange(you: "Osteria Nove.", assistant: "Booked for 7. The invite's on screen. Tap Send when it looks right.",
                              replyStarted: true)
        var gameQuiet = gameTask; gameQuiet.info.result = WorkResult(spoken: "The Knicks beat Boston 112 to 104.", full: nil)
        s.tasks = [gameQuiet, dinnerDraft]; s.runID = "run_d"; s.workInfo = dinnerDraft.info
        out.append(("08-draft", s))
        return out.map { name, state in var st = state; st.work = deriveWork(st, now: now); return (name, st) }
    }

    /// Website screenshots: one full-looking call per shot, everyday asks, invented data.
    static func siteBeats() -> [(String, VoiceState, String?)] {
        let now = Date()
        func base() -> VoiceState {
            var s = VoiceState(); s.now = now; s.connection = .live; s.interactionID = "vi_site"; s.speech = .speaking(lastDelta: now); return s
        }
        let signupChart = card(["kind": "chart", "title": "Signups per hour, yesterday", "style": "bar",
                                "labels": ["10", "11", "12", "1", "2", "3", "4", "5", "6"],
                                "series": [["name": "Signups", "values": [42, 45, 40, 44, 6, 4, 31, 47, 43]]]])
        let signups = task("s", "completed", "Signups look low yesterday. What happened?", title: "Signups dip yesterday",
                           short: "Done", age: 30, now: now,
                           result: WorkResult(spoken: "The signup page was broken for two hours after Tuesday's deploy. It's back to normal.",
                                              full: nil, views: [signupChart]),
                           milestones: ["Signups down 38% from a normal Wednesday", "Signup page errored from 2 to 4 PM",
                                        "Started with Tuesday's deploy, fixed at 4"])
        let weather = card(["kind": "weather", "place": "Brooklyn", "temp": 41, "feels_like": 36, "unit": "F",
                            "condition": "Rain until 3 PM", "icon": "rain", "high": 47, "low": 38])
        let weatherTask = task("w", "completed", "Do I need an umbrella today?", title: "Umbrella today?", short: "Done",
                               age: 3, now: now,
                               result: WorkResult(spoken: "Yes, rain until about 3, then it clears. High of 47.", full: nil,
                                                  views: [weather]))
        let testReq = "The checkout test keeps failing at random. Find out why and fix it."
        let flaky = task("t", "working", testReq, title: "Fix the flaky checkout test", short: "Running it 50 times to be sure",
                         age: 25, now: now, milestones: ["Reproduced it: fails about 1 run in 6",
                                                         "Found it: the test doesn't wait for the payment step",
                                                         "Added the wait"])
        let pricingReq = "Compare our pricing to our top three competitors"
        let pricing = task("p", "working", pricingReq, title: "Pricing vs. competitors",
                           short: "Reading 3 pricing pages", age: 4, now: now, milestones: ["Found the top 3 by search share"])
        let acmeReq = "Reply to Acme: we fixed the invoice, apologize, and attach the new one"
        let draft = EmailDraft(draftID: "site_draft", sha256: "s1te", from: "you@example.com",
                               to: ["billing@acme.example"], cc: [], subject: "Re: Invoice #1042",
                               body: "Hi Priya,\n\nYou're right, invoice #1042 charged you for 30 seats instead of 25. Sorry about that. The corrected invoice is attached, and the $95 difference has been credited to your account.\n\nThanks for flagging it,\nAlex")
        var invite = task("a", "waiting_for_approval", acmeReq, title: "Acme invoice reply",
                          short: "Ready to send, with the corrected invoice", age: 8, now: now,
                          milestones: ["Found the overcharge: 30 seats billed, 25 used", "Made the corrected invoice", "Drafted the reply"])
        invite.info.emailDrafts = [draft]
        let question = card(["kind": "question", "question": "The dashboard takes 9 seconds to load. How should I fix it?",
                             "context": "Almost all of that time is one database query that reads every order to add up today's totals.",
                             "why": "An index fixes the actual cause, needs no code changes, and can be dropped in seconds if anything looks off.",
                             "affects": "Sales dashboard · orders database",
                             "options": [
                                ["text": "Add an index · about 20 min", "detail": "Loads in under a second. No code changes.",
                                 "risk": "low", "reversible": true],
                                ["text": "Cache the totals · about 2 hours", "detail": "Also fast, but numbers can be 5 minutes behind.",
                                 "risk": "medium", "reversible": true],
                                ["text": "Leave it", "detail": "Nothing changes."]],
                             "recommended": 0])
        let loadChart = card(["kind": "chart", "title": "Load time, last 7 days", "style": "line", "unit": "s",
                              "labels": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                              "series": [["name": "Dashboard", "values": [3.1, 3.4, 4.2, 5.8, 7.1, 8.3, 9.2]]]])
        let dashReq = "The sales dashboard is really slow. Figure out why."
        let pick = task("d", "completed", dashReq, title: "Speed up the dashboard", short: "Needs your pick", age: 3, now: now,
                        result: WorkResult(spoken: "One query reads every order. An index would get it under a second.", full: nil,
                                           views: [question, loadChart]),
                        milestones: ["Timed every query on the page", "One query takes 8.7 of the 9 seconds"])

        var out: [(String, VoiceState, String?)] = []
        var s = base()
        s.exchange = Exchange(you: "And check our pricing against competitors.",
                              assistant: "On it. The Acme reply is ready to look at.", replyStarted: true)
        s.tasks = [weatherTask, signups, flaky, invite, pick, pricing]; s.runID = "run_p"; s.delegationAt = now - 4
        s.workInfo = pricing.info
        out.append(("site-board", s, nil))
        s = base()
        s.exchange = Exchange(you: "How's the test fix going?", assistant: "Found it. Running it 50 times to be sure.", replyStarted: true)
        s.tasks = [flaky]; s.runID = "run_t"; s.workInfo = flaky.info
        out.append(("site-working", s, "task:t"))
        s = base()
        s.exchange = Exchange(you: "Why did signups drop yesterday?", assistant: "The signup page was broken for two hours.", replyStarted: true)
        s.tasks = [signups]; s.workInfo = signups.info
        out.append(("site-answer", s, "task:s"))
        s = base()
        s.exchange = Exchange(you: "Do I need an umbrella today?", assistant: "Yes, rain until about 3.", replyStarted: true)
        s.tasks = [weatherTask]; s.workInfo = weatherTask.info
        out.append(("site-quick", s, "task:w"))
        s = base()
        s.exchange = Exchange(you: "", assistant: "I'd add the index. Want me to?", replyStarted: true)
        s.tasks = [flaky, pick]; s.runID = "run_d"; s.workInfo = pick.info
        out.append(("site-decide", s, "task:d"))
        s = base()
        s.exchange = Exchange(you: "Show me the Acme reply.", assistant: "It's on screen. Tap Send when it looks right.", replyStarted: true)
        s.tasks = [flaky, invite]; s.workInfo = invite.info
        out.append(("site-draft", s, "task:a"))
        return out.map { name, state, open in var st = state; st.work = deriveWork(st, now: now); return (name, st, open) }
    }

    /// The UX states (`SPEAKEASY_FILM_SET=ux`): the four voice states, mic problems, decisions
    /// (compare, undo), approvals, progress, failure, card actions, a light answer page. Invented data.
    static func uxBeats() -> [Shot] {
        let now = Date()
        func base(_ speaking: Bool = false) -> VoiceState {
            var s = VoiceState(); s.now = now; s.connection = .live; s.interactionID = "vi_ux"
            if speaking { s.speech = .speaking(lastDelta: now) }
            return s
        }
        func done(_ s: VoiceState) -> VoiceState { var st = s; st.work = deriveWork(st, now: now); return st }
        var out: [Shot] = []

        // 1–4: the four voice states.
        out.append(Shot("ux-01-listening", done(base()), nil))
        var s = base(); s.exchange = Exchange(you: "What's on my calendar tomorrow", assistant: ""); s.lastInputAt = now
        out.append(Shot("ux-02-hearing", done(s), nil) { $0.orbLevel = 0.75 })
        s = base(); s.exchange = Exchange(you: "What's on my calendar tomorrow?", assistant: ""); s.lastInputAt = now - 1
        out.append(Shot("ux-03-thinking", done(s), nil))
        s = base(true)
        s.exchange = Exchange(you: "What's on my calendar tomorrow?",
                              assistant: "Three things. Standup at 9:30, then a design review at 11 that runs an hour. "
                                + "The afternoon is clear until a call with the hiring panel at 4, so you have a good block for the launch notes.",
                              replyStarted: true)
        out.append(Shot("ux-04-speaking", done(s), nil) { $0.orbLevel = 0.6 })

        // 5–6: mic problems with a fix.
        s = base(); s.connection = .failed("Microphone access is off — enable Speakeasy in System Settings › Privacy & Security › Microphone")
        s.lastError = "Microphone access is off — enable Speakeasy in System Settings › Privacy & Security › Microphone"
        out.append(Shot("ux-05-mic-permission", done(s), nil))
        s = base(); s.micHealth = .broken
        out.append(Shot("ux-06-mic-broken", done(s), nil))

        // 7–9: a decision on a run that's still going: the board says Needs you; compare; undo.
        let q = card(["kind": "question", "question": "Which caching layer should the API use?",
                      "context": "Product pages call the pricing service on every view; it's 60% of API time.",
                      "why": "In-process is the least to run and can be turned off with one flag.",
                      "options": [
                        ["text": "In-process cache", "detail": "Fastest to ship. Each server warms its own copy.",
                         "risk": "low", "reversible": true, "tags": ["~1 hour"]],
                        ["text": "Shared Redis", "detail": "One copy for every server. A new service to run.",
                         "risk": "medium", "reversible": true, "tags": ["~half a day"]],
                        ["text": "Leave it", "detail": "Nothing changes."]],
                      "recommended": 0])
        var ask = task("c", "working", "Speed up product pages", title: "Cache pricing calls", short: "Needs your pick",
                       age: 40, now: now, result: WorkResult(spoken: nil, full: nil, views: [q]),
                       milestones: ["Profiled the product page", "Pricing calls are 60% of API time"])
        ask.info.updatedAt = now - 5
        let running = progressTask(now: now)
        s = base(); s.tasks = [running, ask]
        out.append(Shot("ux-07-decide-board", done(s), nil))
        out.append(Shot("ux-08-decide-compare", done(s), "task:c") { $0.openSections.insert("c|compare") })
        out.append(Shot("ux-09-decide-undo", done(s), "task:c") { m in
            m.answers["c"] = "In-process cache"; m.pendingPicks["c"] = Date().addingTimeInterval(4)
        })

        // 10: an approval with its context, real Approve/Deny on the tile.
        var deploy = task("a", "waiting_for_approval", "Ship the pricing cache to staging", title: "Deploy to staging",
                          short: "Waiting for your OK", age: 20, now: now, milestones: ["Tests pass", "Built the image"])
        deploy.info.updatedAt = now - 3
        s = base(); s.tasks = [running, deploy]
        s.approval = ApprovalInfo(runID: "run_a", requestID: "req_1", description: "Run the staging deploy script",
                                  command: "./deploy.sh staging", reason: "The cache change is ready and tests pass.")
        out.append(Shot("ux-10-approval", done(s), nil))

        // 11–12: progress with steps, elapsed and ETA; a failure with its reason and Retry.
        s = base(); s.tasks = [running]; s.runID = "run_p"; s.workInfo = running.info
        out.append(Shot("ux-11-progress", done(s), "task:p"))
        var failed = task("f", "failed", "Renew the TLS certificate for the docs site", title: "Renew docs certificate",
                          short: "Failed", age: 90, now: now, milestones: ["Requested a new certificate"])
        failed.info.failureReason = "The DNS check failed: the docs domain doesn't point at this server yet."
        s = base(); s.tasks = [running, failed]
        out.append(Shot("ux-12-failed", done(s), nil))
        out.append(Shot("ux-12b-failed-detail", done(s), "task:f"))

        // 13–14: card actions: a device toggle (server action) with its toast; a chart and a place.
        let lamp = card(["kind": "home", "title": "Office", "devices": [
            ["name": "Desk lamp", "kind": "light", "on": true, "level": 20],
            ["name": "Heater", "kind": "climate", "on": false, "state": "Off"]],
            "actions": [["id": "toggle", "label": "Turn off lamp", "style": "primary"],
                        ["id": "dim", "label": "Back to 60%", "value": 60]]])
        let homeTask = task("h", "completed", "Is the office lamp on?", title: "Office lights", short: "Done", age: 2, now: now,
                            result: WorkResult(spoken: "The desk lamp is on, dimmed to 20%.", full: nil, views: [lamp]))
        s = base(); s.tasks = [homeTask]; s.workInfo = homeTask.info
        out.append(Shot("ux-13-card-actions", done(s), "task:h") { $0.showToast("Desk lamp dimmed to 20%.") })
        let chart = card(["kind": "chart", "title": "API p95 latency", "style": "line", "unit": "ms",
                          "labels": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                          "series": [["name": "p95", "values": [420, 410, 455, 180, 175]]]])
        let place = ViewCard(json: ["kind": "place", "name": "Corner Coffee", "address": "100 Main St", "rating": 4.6,
                                    "category": "Cafe"], number: 2)!
        let mixed = task("m", "completed", "Did the cache help, and where's a coffee near the office?", title: "Cache results",
                         short: "Done", age: 4, now: now,
                         result: WorkResult(spoken: "Latency dropped by more than half after Wednesday's change.", full: nil,
                                            views: [chart, place]))
        s = base(); s.tasks = [mixed]; s.workInfo = mixed.info
        out.append(Shot("ux-14-chart-place", done(s), "task:m"))
        var scrub = mixed; scrub.id = "n"
        s = base(); s.tasks = [scrub]; s.workInfo = scrub.info
        ChartPreview.selection = nil
        out.append(Shot("ux-14b-chart-scrub", done(s), "task:n") { _ in ChartPreview.selection = "Thu" })

        // 15: a light answer page: headline, cards, key points, the long text folded.
        var report = WorkResult(spoken: "The nightly backup failed twice this week, both times on the photos volume.",
                                full: "I read the backup logs for the last seven nights. Five runs finished normally. "
                                  + "Tuesday and Thursday stopped about forty minutes in, both while copying the photos volume. "
                                  + "The error both times was a timeout talking to the storage bucket, and both runs overlapped with the "
                                  + "weekly media re-index, which saturates the disk. Moving the backup an hour later, or the re-index "
                                  + "to the weekend, would stop them overlapping. Nothing was lost: each failed night was covered by the next run.",
                                views: [card(["kind": "stats", "title": "Backups, last 7 nights",
                                              "items": [["label": "Finished", "value": "5"], ["label": "Failed", "value": "2"],
                                                        ["label": "Data lost", "value": "None"]]])])
        report.summaryPoints = ["2 of 7 nights failed, both on photos", "Both overlapped the weekly re-index",
                                "Timeouts talking to the storage bucket", "Nothing lost: next run covered it"]
        let backup = task("b", "completed", "Why did the backup fail?", title: "Backup failures", short: "Done", age: 5, now: now,
                          result: report)
        s = base(); s.tasks = [backup]; s.workInfo = backup.info
        out.append(Shot("ux-15-light-answer", done(s), "task:b"))
        return out
    }

    static func progressTask(now: Date) -> TaskItem {
        var t = task("p", "working", "Migrate the search index to the new cluster", title: "Search index migration",
                     short: "Copying shard 3 of 5", age: 10, now: now,
                     milestones: ["Snapshotted the old index", "Created the new cluster", "Copying shards"])
        t.info.plan = ["Snapshot the old index", "Create the new cluster", "Copy the shards", "Switch reads over", "Remove the old index"]
        t.info.startedAt = now - 14 * 60
        t.info.stepIndex = 3; t.info.stepCount = 5; t.info.etaSeconds = 9 * 60
        // Steps logged at different times, so they don't all read the same.
        t.info.events = [WorkEventItem(kind: "request", text: "Migrate the search index", at: now - 14 * 60),
                         WorkEventItem(kind: "milestone", text: "Snapshotted the old index", at: now - 12 * 60),
                         WorkEventItem(kind: "milestone", text: "Created the new cluster", at: now - 7 * 60),
                         WorkEventItem(kind: "milestone", text: "Copying shards", at: now - 2 * 60)]
        return t
    }
}
