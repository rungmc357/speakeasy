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
            let site = ProcessInfo.processInfo.environment["SPEAKEASY_FILM_SET"] == "site"
            cleanShots = site
            let shots: [(String, VoiceState, String?)] = site ? siteBeats() : beats().map { ($0.0, $0.1, nil) }
            for (name, state, open) in shots {
                client.showPreview(state, workExpanded: false)
                client.model.onOpenJob(open)
                client.model.boardFocusID = nil
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
        s = base(); s.exchange = Exchange(you: "What's Nvidia at?", assistant: "", replyStarted: false)
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
            var s = VoiceState(); s.now = now; s.connection = .live; s.interactionID = "vi_site"; return s
        }
        let flightsReq = "Find me flights to Lisbon for the 14th, back the 21st"
        let flights = task("f", "completed", flightsReq, title: "Lisbon flights", short: "Done", age: 40, now: now,
                           result: WorkResult(spoken: "Best one's a nonstop on the 14th, $612 round trip, landing at 7 AM.",
                                              full: nil, views: []),
                           milestones: ["Searched 4 airlines", "Nonstop, $612 round trip"])
        let weather = card(["kind": "weather", "place": "Brooklyn", "temp": 41, "feels_like": 36, "unit": "F",
                            "condition": "Rain until 3 PM", "icon": "rain", "high": 47, "low": 38])
        let weatherTask = task("w", "completed", "Do I need an umbrella today?", title: "Umbrella today?", short: "Done",
                               age: 3, now: now,
                               result: WorkResult(spoken: "Yes, rain until about 3, then it clears. High of 47.", full: nil,
                                                  views: [weather]))
        let deskReq = "Find a standing desk under $600 that fits a 48-inch wall"
        let desk = task("k", "working", deskReq, title: "Standing desk under $600", short: "Reading reviews of the last 4",
                        age: 25, now: now, milestones: ["Searched 6 stores", "Found 11 that fit a 48-inch wall", "Cut to 4 under $600 with good reviews"])
        let giftReq = "Find Mom a birthday gift that gets there by Saturday"
        let gift = task("g", "working", giftReq, title: "Mom's birthday gift", short: "Checking what arrives by Saturday",
                        age: 4, now: now, milestones: ["Looked at what she liked last year", "Checking what arrives by Saturday"])
        let dinnerReq = "Book dinner for four near the office Thursday and send the team an invite"
        let draft = EmailDraft(draftID: "site_draft", sha256: "s1te", from: "you@example.com",
                               to: ["team@example.com"], cc: [], subject: "Dinner Thursday, 7 PM",
                               body: "Hi all,\n\nI booked a table for four at Osteria Nove this Thursday at 7. It's a five-minute walk from the office.\n\nSee you there!")
        var invite = task("d", "waiting_for_approval", dinnerReq, title: "Team dinner Thursday",
                          short: "Booked 7 PM · invite ready", age: 8, now: now,
                          milestones: ["Booked Osteria Nove for 7 PM", "Drafted the invite"])
        invite.info.emailDrafts = [draft]
        let question = card(["kind": "question", "question": "Three places have a table for four at 7. Which one?",
                             "context": "All within a 10-minute walk of the office, all take reservations for Thursday.",
                             "options": [
                                ["text": "Osteria Nove", "detail": "Italian · 5 min walk · quiet back room",
                                 "tags": [["text": "Best for talking", "tone": "good"]]],
                                ["text": "Hearth & Vine", "detail": "New American · 8 min walk · busier, bar seating"],
                                ["text": "Baan Lime", "detail": "Thai · 6 min walk · cheapest of the three"]],
                             "recommended": 0])
        let pick = task("d", "completed", dinnerReq, title: "Team dinner Thursday", short: "Needs your pick", age: 3, now: now,
                        result: WorkResult(spoken: "Three places have a table at 7. I'd go with Osteria Nove.", full: nil,
                                           views: [question]),
                        milestones: ["Checked 9 places near the office", "Three have a table at 7"])

        var out: [(String, VoiceState, String?)] = []
        // The board: one call, a few things at once.
        var s = base(); s.speech = .speaking(lastDelta: now)
        s.exchange = Exchange(you: "And a gift for Mom by Saturday.",
                              assistant: "On it. The dinner invite's ready.", replyStarted: true)
        s.tasks = [weatherTask, flights, desk, invite, gift]; s.runID = "run_g"; s.delegationAt = now - 4
        s.workInfo = gift.info
        out.append(("site-board", s, nil))
        // Watch it work: a running job opened.
        s = base(); s.speech = .speaking(lastDelta: now)
        s.exchange = Exchange(you: "How's the desk search going?", assistant: "Down to four. Comparing reviews now.", replyStarted: true)
        s.tasks = [desk]; s.runID = "run_k"; s.workInfo = desk.info
        out.append(("site-working", s, "task:k"))
        // A quick answer with its card.
        s = base(); s.speech = .speaking(lastDelta: now)
        s.exchange = Exchange(you: "Do I need an umbrella today?",
                              assistant: "Yes, rain until about 3.", replyStarted: true)
        s.tasks = [weatherTask]; s.workInfo = weatherTask.info
        out.append(("site-answer", s, "task:w"))
        // A decision.
        s = base(); s.speech = .speaking(lastDelta: now)
        s.exchange = Exchange(you: "", assistant: "I'd go with Osteria Nove.", replyStarted: true)
        s.tasks = [desk, pick]; s.runID = "run_d"; s.workInfo = pick.info
        out.append(("site-decide", s, "task:d"))
        // The draft, waiting for you.
        s = base(); s.speech = .speaking(lastDelta: now)
        s.exchange = Exchange(you: "Osteria Nove.", assistant: "Booked. Invite's on screen.", replyStarted: true)
        s.tasks = [desk, invite]; s.workInfo = invite.info
        out.append(("site-draft", s, "task:d"))
        return out.map { name, state, open in var st = state; st.work = deriveWork(st, now: now); return (name, st, open) }
    }
}
