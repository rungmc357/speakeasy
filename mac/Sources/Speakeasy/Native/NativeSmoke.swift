import AppKit
import AVFoundation
import Foundation
import SpeakeasyCore
import SpeakeasyClient

/// Offline smoke checks and canned UI previews. None of these touch the network.
@MainActor
enum NativeSmoke {
    /// Build a real local offer (receive-only transceiver, no mic), print its shape.
    static func offer() {
        let engine = NativeCallEngine()
        Task { @MainActor in
            do {
                try engine.prepare(captureAudio: false)
                let sdp = try await engine.createOffer(iceTimeout: 5)
                let lines = sdp.components(separatedBy: .newlines).filter { !$0.isEmpty }
                let audio = lines.contains { $0.hasPrefix("m=audio") }
                let data = lines.contains { $0.hasPrefix("m=application") }
                let crlf = sdp.hasSuffix("\r\n")
                print("native-offer-smoke ok sdp_lines=\(lines.count) bytes=\(sdp.utf8.count) m_audio=\(audio) m_application=\(data) trailing_crlf=\(crlf)")
                engine.close()
                exit(0)
            } catch {
                print("native-offer-smoke failed: \(error.localizedDescription)")
                engine.close()
                exit(1)
            }
        }
    }

    /// Permission + one second of capture; prints peak level. No network.
    static func mic() {
        AVCaptureDevice.requestAccess(for: .audio) { granted in
            guard granted else { print("native-mic-smoke denied"); exit(2) }
            DispatchQueue.main.async {
                let engine = AVAudioEngine()
                let input = engine.inputNode
                let format = input.outputFormat(forBus: 0)
                var peak: Float = 0
                var frames = 0
                let lock = NSLock()
                input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
                    guard let channel = buffer.floatChannelData?[0] else { return }
                    var local: Float = 0
                    for i in 0..<Int(buffer.frameLength) { local = max(local, abs(channel[i])) }
                    lock.lock(); peak = max(peak, local); frames += Int(buffer.frameLength); lock.unlock()
                }
                do { try engine.start() } catch { print("native-mic-smoke failed: \(error.localizedDescription)"); exit(1) }
                DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                    engine.stop(); input.removeTap(onBus: 0)
                    lock.lock(); let p = peak; let f = frames; lock.unlock()
                    print(String(format: "native-mic-smoke ok peak=%.4f frames=%d rate=%.0f", p, f, format.sampleRate))
                    exit(0)
                }
            }
        }
    }
}

/// Canned states for `--ui-preview <state>`.
enum PreviewFixtures {
    /// A drawn stand-in for a Hermes render, so the image card previews without a api.
    static func sampleImage() -> Data? {
        let image = NSImage(size: NSSize(width: 640, height: 400), flipped: false) { rect in
            NSGradient(starting: .systemOrange, ending: .systemPurple)?.draw(in: rect, angle: 35)
            NSColor.white.withAlphaComponent(0.85).setFill()
            NSBezierPath(ovalIn: NSRect(x: 250, y: 120, width: 140, height: 180)).fill()
            return true
        }
        guard let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff) else { return nil }
        return rep.representation(using: .png, properties: [:])
    }

    /// A drawn stand-in for a finished landing-page design (the design review card).
    static func sampleDesign(variant: Int = 0) -> Data? {
        let image = NSImage(size: NSSize(width: 1200, height: 800), flipped: true) { rect in
            let accents: [NSColor] = [.systemOrange, .systemTeal, .systemPink]
            let accent = accents[abs(variant) % accents.count]
            NSColor(calibratedWhite: 0.98, alpha: 1).setFill(); rect.fill()
            NSColor(calibratedWhite: 0.12, alpha: 1).setFill(); NSRect(x: 0, y: 0, width: 1200, height: 70).fill()
            let nav: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 26, weight: .semibold), .foregroundColor: NSColor.white]
            ("Crumb & Co." as NSString).draw(at: NSPoint(x: 48, y: 20), withAttributes: nav)
            accent.withAlphaComponent(0.18).setFill(); NSRect(x: 0, y: 70, width: 1200, height: 380).fill()
            let h1: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 64, weight: .bold), .foregroundColor: NSColor(calibratedWhite: 0.1, alpha: 1)]
            ("Fresh bread, every morning." as NSString).draw(at: NSPoint(x: 72, y: 150), withAttributes: h1)
            let sub: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 28), .foregroundColor: NSColor.darkGray]
            ("Order by 8 pm, pick up at 7 am." as NSString).draw(at: NSPoint(x: 76, y: 250), withAttributes: sub)
            accent.setFill(); NSBezierPath(roundedRect: NSRect(x: 76, y: 320, width: 240, height: 64), xRadius: 32, yRadius: 32).fill()
            let btn: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 26, weight: .semibold), .foregroundColor: NSColor.white]
            ("Order now" as NSString).draw(at: NSPoint(x: 134, y: 336), withAttributes: btn)
            for i in 0..<3 {
                let box = NSRect(x: 72 + i * 360, y: 500, width: 330, height: 250)
                NSColor(calibratedWhite: 0.92, alpha: 1).setFill(); NSBezierPath(roundedRect: box, xRadius: 18, yRadius: 18).fill()
                accent.withAlphaComponent(0.55).setFill(); NSBezierPath(ovalIn: NSRect(x: box.minX + 115, y: box.minY + 40, width: 100, height: 100)).fill()
                let t: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 24, weight: .medium), .foregroundColor: NSColor.darkGray]
                (["Sourdough", "Baguettes", "Croissants"][i] as NSString).draw(at: NSPoint(x: box.minX + 100, y: box.minY + 170), withAttributes: t)
            }
            return true
        }
        guard let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff) else { return nil }
        return rep.representation(using: .png, properties: [:])
    }

    static let names = ["listening", "speaking", "muted", "working", "waiting", "stale", "approval", "done", "expanded", "tasks", "tasklist", "paused", "products", "image", "design-review", "looking", "email-draft", "ended", "home", "failed", "busy", "text-reply", "text-reply-compact"] + jobNames

    static func state(_ name: String) -> (VoiceState, workExpanded: Bool)? {
        if let job = jobState(name) { return job }
        let now = Date()
        var s = VoiceState()
        s.now = now
        s.connection = .live
        s.interactionID = "vi_preview"
        let request = WorkEventItem(kind: "request", text: "Can you check whether my UA 123 flight tomorrow is still on time and text Dana if it moved?", at: now - 95)
        func work(_ status: String, short: String?, age: TimeInterval, stale: Bool = false, result: WorkResult? = nil) -> WorkInfo {
            WorkInfo(runID: "run_preview", status: status, stale: stale, updated: now - age,
                     shortStatus: short, detail: "Comparing United's live status page with FlightAware",
                     updatedAt: now - age, statusSource: "authored",
                     events: [request,
                              WorkEventItem(kind: "milestone", text: "Opened United flight status", at: now - 80),
                              WorkEventItem(kind: "milestone", text: "Found departure gate C71", at: now - 40)],
                     result: result)
        }
        switch name {
        case "listening":
            s.exchange = Exchange(you: "What's on my calendar after lunch?", assistant: "", replyStarted: false)
        case "speaking":
            s.speech = .speaking(lastDelta: now)
            s.exchange = Exchange(you: "What's on my calendar after lunch?",
                                  assistant: "You have the design review at two, then a call with Dana at four thirty.", replyStarted: true)
        case "muted":
            s.mic = .muted
        case "working":
            s.runID = "run_preview"; s.delegationAt = now - 95
            s.workInfo = work("working", short: "Checking UA 123 status", age: 4)
            s.exchange = Exchange(you: "Is my flight tomorrow on time?", assistant: "I'll check United and let you know.", replyStarted: true)
        case "waiting":
            s.delegationAt = now - 92
            s.exchange = Exchange(you: "Is my flight tomorrow on time?", assistant: "On it.", replyStarted: true)
        case "stale":
            s.runID = "run_preview"; s.delegationAt = now - 300
            s.workInfo = work("working", short: "Checking UA 123 status", age: 140)
        case "approval":
            s.runID = "run_preview"; s.delegationAt = now - 120
            s.workInfo = work("waiting_for_approval", short: "Ready to text Dana", age: 6)
            s.approval = ApprovalInfo(runID: "run_preview", requestID: "req_preview",
                                      description: "Send an iMessage to Dana: “Flight moved to 9:40, landing 12:15.”")
        case "done":
            s.runID = "run_preview"; s.delegationAt = now - 200
            s.workInfo = work("completed", short: "Done", age: 3,
                              result: WorkResult(spoken: "UA 123 is on time",
                                                 full: "United 123 departs Newark (EWR) at 9:05 AM from gate C71 and is on time.\nLive status: https://www.united.com/en/us/flightstatus",
                                                 label: "Flight checked"))
            s.exchange = Exchange(you: "Is my flight tomorrow on time?", assistant: "Yes — UA 123 is on time from gate C71.", replyStarted: true)
        case "products":
            let raw: [String: Any] = ["name": "KEF LSX II LT", "url": "https://example.com/kef",
                                      "price": "$999", "store": "Example store", "rating": "4.5",
                                      "specs": ["AirPlay 2", "Stereo pair", "Compact"]]
            let product = ProductCard(json: raw, number: 1)!
            let result = WorkResult(spoken: "I put the speaker on screen.",
                                    full: "The KEF LSX II LT is the compact option.", products: [product])
            s.workInfo = work("completed", short: "Speakers compared", age: 3, result: result)
            s.workInfo?.title = "Compact speaker for the office"
            s.workInfo?.events = [WorkEventItem(kind: "request", text: "Find me a compact speaker for the office, under a grand", at: now - 90)]
            s.tasks = [TaskItem(id: "speaker", info: s.workInfo!)]
            s.exchange = Exchange(you: "Find me a compact speaker for the office, under a grand.",
                                  assistant: "The KEF LSX II LT fits. It's on screen.", replyStarted: true)
            s.runID = "run_preview"
        case "image":
            let result = WorkResult(spoken: "The picture is on screen.", full: "Here's concept 6, the voxel flame.",
                                    label: "Concept drawn", images: [ImageCard(number: 1, name: "voxel-flame-concept-06.png")])
            s.workInfo = work("completed", short: "Concept drawn", age: 3, result: result)
            s.tasks = [TaskItem(id: "concept", info: s.workInfo!)]
            s.runID = "run_preview"
        case "tasks", "tasklist", "paused":
            func item(_ id: String, _ status: String, _ text: String, title: String?, short: String?, age: TimeInterval,
                      result: WorkResult? = nil) -> TaskItem {
                TaskItem(id: id, info: WorkInfo(runID: "run_\(id)", status: status, updated: now - age, shortStatus: short,
                                                updatedAt: now - age, statusSource: "authored",
                                                events: [WorkEventItem(kind: "request", text: text, at: now - age - 60)],
                                                result: result, title: title))
            }
            s.tasks = [
                item("d0", "completed", "Where did we leave off with the iOS app?", title: "iOS app status", short: "Done", age: 90,
                     result: WorkResult(spoken: "Built, never run on your phone.", full: nil, label: nil)),
                item("d1", "completed", "What's the weather tomorrow in Brooklyn?", title: "Brooklyn weather tomorrow", short: "Done", age: 40,
                     result: WorkResult(spoken: "Sunny, 72.", full: "Sunny, high of 72.", label: "Weather checked")),
                item("d2", "working", "Restock the snack cart with salty stuff, about $60", title: "Restock snack cart", short: "Comparing Costco and Amazon", age: 5),
                item("d3", "waiting_for_approval", "Text Dana that I'm running late", title: nil, short: "Ready to text Dana", age: 3),
            ]
            s.runID = "run_d2"; s.delegationAt = now - 65
            s.workInfo = s.tasks[2].info
            s.exchange = Exchange(you: "Salty, around sixty bucks.", assistant: "Got it — looking into that now.", replyStarted: true)
            if name == "paused" { s.connection = .paused; s.resumeFrom = "vi_preview" }
        case "busy":
            // A long call: several finished tasks, then one that asks a question and lists what's
            // open. The question must stay in view; finished tasks fold away.
            func done(_ id: String, _ title: String, _ label: String?, age: TimeInterval, views: [ViewCard] = []) -> TaskItem {
                TaskItem(id: id, info: WorkInfo(runID: "run_\(id)", status: "completed", updated: now - age, shortStatus: "Done",
                                                updatedAt: now - age, statusSource: "authored",
                                                events: [WorkEventItem(kind: "request", text: title, at: now - age - 30)],
                                                result: WorkResult(spoken: "Done.", full: nil, label: label, views: views),
                                                title: title))
            }
            let question = ViewCard(kind: "question", number: 1, data: [
                "question": "Want me to update the laptop app and fix the retry setting now?",
                "options": ["Do both", "Just the app update", "Not now"], "recommended": 0])
            let open = ViewCard(kind: "list", number: 2, data: ["title": "Worth picking up", "items": [
                ["text": "Restart the desktop", "done": false], ["text": "First real test recording", "done": false],
                ["text": "Update the app on the laptop", "done": false], ["text": "Retry setting: 3 errors / 30s", "done": false],
                ["text": "Hermes update", "done": true]]])
            s.tasks = [
                done("b0", "Hermes update changelog", nil, age: 300),
                done("b1", "How do you know", nil, age: 240),
                done("b2", "Review recent work and projects", "Recent work reviewed", age: 120),
                done("b3", "Context on what to pick up", "Open loops reviewed", age: 20, views: [question, open]),
            ]
            s.runID = "run_b3"
            s.workInfo = s.tasks[3].info
            s.exchange = Exchange(you: "Okay, that seems reasonable.", assistant: "The tested release, not every commit in between.",
                                  replyStarted: true)
        case "home":
            // Instant home control: the lights and heat are done before the reply ends, while a
            // bigger task keeps running next to it.
            func item(_ id: String, _ status: String, _ text: String, title: String, short: String, age: TimeInterval,
                      result: WorkResult? = nil) -> TaskItem {
                TaskItem(id: id, info: WorkInfo(runID: "run_\(id)", status: status, updated: now - age, shortStatus: short,
                                                updatedAt: now - age, statusSource: "authored",
                                                events: [WorkEventItem(kind: "request", text: text, at: now - age - 1)],
                                                result: result, title: title))
            }
            s.tasks = [
                item("h0", "working", "Find a Thai place that delivers after ten", title: "Late-night Thai delivery",
                     short: "Checking who's still open", age: 40),
                item("h1", "completed", "Kitchen lights to thirty percent and the living room to seventy-two",
                     title: "Kitchen lights and heat", short: "Done", age: 2,
                     result: WorkResult(spoken: "Kitchen's at 30 percent and the living room is set to 72.", full: nil, label: nil)),
            ]
            s.runID = "run_h0"; s.delegationAt = now - 3
            s.workInfo = s.tasks[0].info
            s.exchange = Exchange(you: "Kitchen lights to 30, and heat to 72.",
                                  assistant: "Done. Anything else?", replyStarted: true)
        case "failed":
            // Hermes' model provider ran out of credits (HTTP 402): the row and the detail say so
            // in the plugin's words, next to a task that still finished.
            var failed = WorkInfo(runID: "run_f1", status: "failed", updated: now - 2,
                                  events: [WorkEventItem(kind: "request", text: "What's the weather tomorrow in Lisbon?", at: now - 3)],
                                  title: "Lisbon weather tomorrow")
            failed.failure = WorkFailure(kind: "billing", label: "Out of credits",
                                         text: "Your AI account is out of credits. Add credits there, or switch model in this chat.")
            let done = WorkInfo(runID: "run_f0", status: "completed", updated: now - 60, shortStatus: "Done",
                                events: [WorkEventItem(kind: "request", text: "Kitchen lights to thirty percent", at: now - 61)],
                                result: WorkResult(spoken: "Kitchen's at 30 percent.", full: nil), title: "Kitchen lights")
            s.tasks = [TaskItem(id: "f0", info: done), TaskItem(id: "f1", info: failed)]
            s.runID = "run_f1"; s.delegationAt = now - 3
            s.workInfo = failed
            s.exchange = Exchange(you: "What's the weather tomorrow in Lisbon?",
                                  assistant: "That task didn't finish. Your AI account is out of credits.", replyStarted: true)
        case "detail":
            let events = [
                WorkEventItem(kind: "request", text: "Restock the snack cart with salty stuff, about $60", at: now - 70),
                WorkEventItem(kind: "milestone", text: "Checked what's usually on the cart", at: now - 55),
                WorkEventItem(kind: "milestone", text: "Found chips, pretzels and nuts at Costco", at: now - 38),
                WorkEventItem(kind: "milestone", text: "Same items on Amazon, $8 more with delivery", at: now - 20),
            ]
            s.workInfo = WorkInfo(runID: "run_detail", status: "working", updated: now - 4,
                                  shortStatus: "Building the Costco cart, about $57 so far",
                                  updatedAt: now - 4, statusSource: "authored", events: events, result: nil,
                                  title: "Restock snack cart")
            s.tasks = [TaskItem(id: "detail", info: s.workInfo!)]
            s.runID = "run_detail"; s.delegationAt = now - 70
        case "design-review":
            let images = (1...3).map { ImageCard(number: $0, name: "bakery-landing-v\($0).png") }
            s.workInfo = work("completed", short: "Design done", age: 3,
                              result: WorkResult(spoken: "The landing page is ready.", full: "Three takes on the bakery landing page.",
                                                 label: "Design done", images: images))
            s.workInfo?.title = "Bakery landing page"
            s.workInfo?.reviewImages = [1, 2, 3]
            s.workInfo?.reviewSettledAt = now - 3
            s.tasks = [TaskItem(id: "design", info: s.workInfo!)]
            s.runID = "run_preview"
            s.exchange = Exchange(you: "Design a landing page for the bakery.", assistant: "It's ready — have a look.", replyStarted: true)
        case "looking":
            s.runID = "run_looking"; s.delegationAt = now - 50
            s.workInfo = WorkInfo(runID: "run_looking", status: "working", updated: now - 3,
                                  shortStatus: "Checking the new hero on mobile", updatedAt: now - 3, statusSource: "authored",
                                  events: [WorkEventItem(kind: "request", text: "Tighten the bakery landing page hero", at: now - 50),
                                           WorkEventItem(kind: "milestone", text: "Opened the page in the browser", at: now - 30)],
                                  title: "Bakery hero tweaks")
            s.workInfo?.liveImage = LiveImage(name: "browser_screenshot_3f2a.png", source: "screenshot", seq: 3, at: now - 3)
            var other = WorkInfo(runID: "run_other", status: "working", updated: now - 8, shortStatus: "Comparing flour suppliers",
                                 updatedAt: now - 8, statusSource: "authored",
                                 events: [WorkEventItem(kind: "request", text: "Find a cheaper flour supplier", at: now - 70)],
                                 title: "Flour suppliers")
            other.liveImage = nil
            s.tasks = [TaskItem(id: "other", info: other), TaskItem(id: "looking", info: s.workInfo!)]
            s.exchange = Exchange(you: "What are you looking at?", assistant: "It's on your screen.", replyStarted: true)
        case "email-draft":
            let draft = EmailDraft(draftID: "draft_preview", sha256: "0f3a9c", from: "you@example.com",
                                   to: ["dana@example.com"], cc: ["sam@example.com"],
                                   subject: "Running a little late",
                                   body: "Hi Dana,\n\nMy flight moved to 9:40, so I'll land around 12:15. Could we push lunch to 1:30?\n\nThanks!")
            s.runID = "run_preview"; s.delegationAt = now - 60
            s.workInfo = work("waiting_for_approval", short: "Drafted an email to Dana", age: 4)
            s.workInfo?.emailDrafts = [draft]
            s.tasks = [TaskItem(id: "email", info: s.workInfo!)]
            s.exchange = Exchange(you: "Email Dana that I'm running late.", assistant: "I drafted it — take a look.", replyStarted: true)
        case "ended":
            s.connection = .ended(.complete)
            s.interactionID = nil
            s.exchange = Exchange(you: "That's all, thanks.", assistant: "Talk soon.", replyStarted: true)
        case "text-reply", "text-reply-compact":
            // A finished task's full written answer in the expanded transcript; the voice said the short line.
            s.speech = .speaking(lastDelta: now)
            s.transcript = [
                TranscriptTurn(id: 0, speaker: .you, text: "Find me flights to Denver on Friday morning."),
                TranscriptTurn(id: 1, speaker: .assistant, text: "On it."),
                TranscriptTurn(textReply: "Three flights fit your window.\n\n1. **UA 123** leaves 8:05, lands 10:40 — $214.\n2. **F9 77** leaves 11:30, lands 2:05 — $129.\n3. **WN 410** leaves 6:15 pm — $188.\n\nFrontier is cheapest; United is the only morning nonstop. [Fares](https://example.com/fares)",
                               id: 2, taskID: "flights", taskName: "Flights to Denver"),
                TranscriptTurn(id: 3, speaker: .assistant, text: "Three flights fit; the cheapest is Frontier at 11:30."),
            ]
            s.exchange = Exchange(you: "Find me flights to Denver on Friday morning.",
                                  assistant: "Three flights fit; the cheapest is Frontier at 11:30.", replyStarted: true)
        case "expanded":
            s.runID = "run_preview"; s.delegationAt = now - 95
            s.workInfo = work("working", short: "Checking UA 123 status", age: 4)
            s.workInfo?.events.append(WorkEventItem(kind: "milestone", text: "Checking inbound aircraft", at: now - 4))
            s.exchange = Exchange(you: "Is my flight tomorrow on time?", assistant: "I'll check.", replyStarted: true)
        default:
            return nil
        }
        s.work = deriveWork(s, now: now)
        return (s, name == "expanded" || name == "tasklist" || name == "products" || name == "image")
    }
}
